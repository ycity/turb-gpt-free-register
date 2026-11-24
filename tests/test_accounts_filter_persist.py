# -*- coding: utf-8 -*-
"""账号筛选状态要能跨刷新保留。

需求（2026-11-18）：挑号时经常要反复刷新看最新数据，每次刷新后
「未订阅 + 过滤2席位」都被重置，得重新点一遍。

实现：存一个 JSON 到 localStorage，初始化时恢复。
存成单个 JSON 而不是多个 key —— 加新筛选时只改一张表，
不用再记得同步 save/load 两处。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


class SourceTest(unittest.TestCase):
    """源码层面的接线检查。"""

    def test_store_key_defined(self):
        self.assertIn("ACCOUNTS_FILTER_STORE_KEY", HTML)

    def test_all_toggles_covered(self):
        """六个筛选按钮都要在持久化表里，漏一个就会"只有部分被记住"。"""
        i = HTML.index("const ACCOUNTS_FILTER_FIELDS")
        seg = HTML[i:HTML.index("];", i)]
        for el in ("#showArchivedAccountsV2", "#showPlusAccountsOnlyV2",
                   "#showUnsubscribedAccountsOnlyV2",
                   "#showPlusTrialAccountsOnlyV2", "#showFreeAccountsOnlyV2",
                   "#hideMultiSeatAccountsV2"):
            self.assertIn(el, seg, "%s 没进持久化表" % el)

    def test_every_apply_saves(self):
        """任何一个改筛选的入口都要落盘，否则那个筛选记不住。"""
        for fn in ("applyAccountsArchivedFilter", "applyAccountsPlanFilter",
                   "applyHideMultiSeatFilter"):
            i = HTML.index("function " + fn)
            body = HTML[i:i + 900]
            self.assertIn("saveAccountsFilters()", body, "%s 漏了保存" % fn)

    def test_restore_called_before_tab_activation(self):
        """activateTab('accounts') 会直接 loadAccounts()，
        晚恢复的话第一次请求带空筛选，页面会闪一下全量列表。"""
        r = HTML.index("restoreAccountsFilters();")
        a = HTML.index("activateTab(localStorage.getItem('gpt_console_active_tab')")
        self.assertLess(r, a, "恢复必须在激活标签页之前")

    def test_buttons_exist_before_restore_runs(self):
        """恢复时要 setAccountsFilterToggle，按钮必须已在 DOM 里。"""
        btn = HTML.index('id="hideMultiSeatAccountsV2"')
        call = HTML.index("restoreAccountsFilters();")
        self.assertLess(btn, call)

    def test_storage_errors_are_swallowed(self):
        """隐私模式下 localStorage 会抛错，不能让筛选本身失效。"""
        for fn in ("function saveAccountsFilters", "function restoreAccountsFilters"):
            i = HTML.index(fn)
            body = HTML[i:i + 700]
            self.assertIn("catch", body, "%s 没有兜底" % fn)


def _node() -> str:
    return shutil.which("node") or ""


@unittest.skipUnless(_node(), "没装 node，跳过真实 JS 执行")
class BehaviourTest(unittest.TestCase):
    """用 Node 真跑这几个函数，验证行为而不是文本。"""

    def _run(self, script_body: str) -> dict:
        # 把三个函数和依赖从模板里抠出来，在最小环境里执行
        def grab(start_marker, end_marker):
            i = HTML.index(start_marker)
            j = HTML.index(end_marker, i)
            return HTML[i:j]

        fields = grab("const ACCOUNTS_FILTER_STORE_KEY",
                      "async function applyAccountsArchivedFilter")
        toggle = grab("function setAccountsFilterToggle(", "\n// ---- 筛选状态持久化")

        harness = """
const __store = {};
const localStorage = {
  getItem: k => (k in __store ? __store[k] : null),
  setItem: (k, v) => { __store[k] = String(v); },
};
let SHOW_ARCHIVED_ACCOUNTS = false;
let SHOW_PLUS_ACCOUNTS_ONLY = false;
let SHOW_UNSUBSCRIBED_ACCOUNTS_ONLY = false;
let SHOW_PLUS_TRIAL_ACCOUNTS_ONLY = false;
let SHOW_FREE_ACCOUNTS_ONLY = false;
let HIDE_MULTI_SEAT_ACCOUNTS = false;
const __els = {};
function $(sel) {
  if (!__els[sel]) __els[sel] = { cls: false, attrs: {},
    classList: { toggle(n, on) { __els[sel].cls = !!on; } },
    setAttribute(k, v) { __els[sel].attrs[k] = v; } };
  return __els[sel];
}
%s
%s
%s
""" % (toggle, fields, script_body)

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(harness)
            path = f.name
        try:
            out = subprocess.run([_node(), path], capture_output=True,
                                 text=True, timeout=30, encoding="utf-8")
            if out.returncode != 0:
                self.fail("node 执行失败：%s" % (out.stderr or "")[:400])
            return json.loads(out.stdout.strip().splitlines()[-1])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_round_trip(self):
        """存了再读，状态要一模一样。"""
        r = self._run("""
SHOW_UNSUBSCRIBED_ACCOUNTS_ONLY = true;
HIDE_MULTI_SEAT_ACCOUNTS = true;
saveAccountsFilters();
SHOW_UNSUBSCRIBED_ACCOUNTS_ONLY = false;
HIDE_MULTI_SEAT_ACCOUNTS = false;
restoreAccountsFilters();
console.log(JSON.stringify({
  unsub: SHOW_UNSUBSCRIBED_ACCOUNTS_ONLY,
  hide: HIDE_MULTI_SEAT_ACCOUNTS,
}));
""")
        self.assertTrue(r["unsub"], "未订阅没被恢复")
        self.assertTrue(r["hide"], "过滤2席位没被恢复")

    def test_combination_preserved(self):
        """「未订阅 + 过滤2席位」这个常用组合要整体保留。"""
        r = self._run("""
SHOW_UNSUBSCRIBED_ACCOUNTS_ONLY = true;
HIDE_MULTI_SEAT_ACCOUNTS = true;
saveAccountsFilters();
restoreAccountsFilters();
console.log(JSON.stringify({
  unsub: SHOW_UNSUBSCRIBED_ACCOUNTS_ONLY,
  hide: HIDE_MULTI_SEAT_ACCOUNTS,
  plus: SHOW_PLUS_ACCOUNTS_ONLY,
}));
""")
        self.assertTrue(r["unsub"])
        self.assertTrue(r["hide"])
        self.assertFalse(r["plus"], "没选的不该被打开")

    def test_empty_storage_is_safe(self):
        """首次访问没有存档，不能报错也不该乱开筛选。"""
        r = self._run("""
const ok = restoreAccountsFilters();
console.log(JSON.stringify({
  ret: ok,
  unsub: SHOW_UNSUBSCRIBED_ACCOUNTS_ONLY,
  hide: HIDE_MULTI_SEAT_ACCOUNTS,
}));
""")
        self.assertFalse(r["ret"])
        self.assertFalse(r["unsub"])
        self.assertFalse(r["hide"])

    def test_corrupt_storage_is_safe(self):
        """存档被写坏时要当成没有，而不是整页崩掉。"""
        r = self._run("""
localStorage.setItem(ACCOUNTS_FILTER_STORE_KEY, '{不是JSON');
const ok = restoreAccountsFilters();
console.log(JSON.stringify({ret: ok, hide: HIDE_MULTI_SEAT_ACCOUNTS}));
""")
        self.assertFalse(r["ret"])
        self.assertFalse(r["hide"])

    def test_button_visual_state_restored(self):
        """变量恢复了但按钮没高亮，用户会以为没生效。"""
        r = self._run("""
HIDE_MULTI_SEAT_ACCOUNTS = true;
saveAccountsFilters();
HIDE_MULTI_SEAT_ACCOUNTS = false;
restoreAccountsFilters();
console.log(JSON.stringify({
  pressed: $('#hideMultiSeatAccountsV2').attrs['aria-pressed'],
  active: $('#hideMultiSeatAccountsV2').cls,
}));
""")
        self.assertEqual(r["pressed"], "true", "按钮没显示成选中态")
        self.assertTrue(r["active"])

    def test_turning_off_persists(self):
        """关掉筛选也要记住，不能刷新后又自己打开。"""
        r = self._run("""
HIDE_MULTI_SEAT_ACCOUNTS = true;
saveAccountsFilters();
HIDE_MULTI_SEAT_ACCOUNTS = false;
saveAccountsFilters();
HIDE_MULTI_SEAT_ACCOUNTS = true;
restoreAccountsFilters();
console.log(JSON.stringify({hide: HIDE_MULTI_SEAT_ACCOUNTS}));
""")
        self.assertFalse(r["hide"], "关掉的筛选又被恢复成开启")


if __name__ == "__main__":
    unittest.main()
