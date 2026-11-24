# -*- coding: utf-8 -*-
"""支付出口可单独指定。

怀疑 Stripe 拒付和「账单地址国家 ≠ 出口 IP 国家」有关：#33 出口在新加坡、
账单地址在美国，自动支付连续失败；同一条长链在人自己的网络里却付成功。
要验证这个假设，就得能在不动注册代理的前提下临时换一个出口重试。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")


class ResolveByNameTest(unittest.TestCase):
    """前端只拿得到名称（列表不下发密码），后端要能还原完整地址。"""

    def setUp(self):
        from webui import proxy_pool
        self.pp = proxy_pool
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = proxy_pool._current_lines
        self.lines = [
            "http://u:p@1.1.1.1:8080#US-Node",
            "http://u:p@2.2.2.2:8080#SG-Node",
            "#http://u:p@3.3.3.3:8080#Disabled-Node",
        ]
        proxy_pool._current_lines = lambda: self.lines

    def tearDown(self):
        self.pp._current_lines = self._orig
        self._tmp.cleanup()

    def test_resolves_full_url_with_credentials(self):
        got = self.pp.resolve_by_name("US-Node")
        self.assertIn("1.1.1.1:8080", got)
        self.assertIn("u:p@", got)

    def test_picks_the_right_entry(self):
        self.assertIn("2.2.2.2", self.pp.resolve_by_name("SG-Node"))

    def test_unknown_name_returns_empty(self):
        self.assertEqual(self.pp.resolve_by_name("Nope"), "")

    def test_blank_name_returns_empty(self):
        self.assertEqual(self.pp.resolve_by_name(""), "")
        self.assertEqual(self.pp.resolve_by_name("   "), "")

    def test_disabled_entry_is_still_resolvable(self):
        """enabled 只管注册时随机抽不抽它；支付是逐单手动指定，实测有些
        被停用的节点反而付得通（Aaitr-ATT 付不通，Aaitr-Frontier 能通）。"""
        got = self.pp.resolve_by_name("Disabled-Node")
        self.assertIn("3.3.3.3:8080", got)


class BackendAcceptsProxyNameTest(unittest.TestCase):
    def test_reads_pay_proxy_name(self):
        self.assertIn('data.get("pay_proxy_name")', APP)

    def test_resolves_through_pool(self):
        """按名字选池里的节点这条路要保留。

        窗口要够大：中间还夹着"按结算国家现取 1024proxy"那个分支。
        """
        idx = APP.index('data.get("pay_proxy_name")')
        self.assertIn("resolve_by_name", APP[idx:idx + 2200])

    def test_unknown_name_is_rejected_not_silently_ignored(self):
        """名字打错就该报错，不能悄悄退回注册代理让人以为换成功了。"""
        idx = APP.index('data.get("pay_proxy_name")')
        window = APP[idx:idx + 2200]
        self.assertIn("找不到出口", window)
        self.assertIn("400", window)

    def test_explicit_url_still_wins(self):
        """已有的 pay_proxy（探测回传的完整地址）优先级不变。"""
        idx = APP.index('data.get("pay_proxy")')
        name_idx = APP.index('data.get("pay_proxy_name")')
        self.assertLess(idx, name_idx)


class FrontendProxyPickerTest(unittest.TestCase):
    def test_selector_exists(self):
        self.assertIn('id="autoPayProxySelect"', HTML)
        self.assertIn('id="autoPayProxyHint"', HTML)

    def test_defaults_to_registration_proxy(self):
        idx = HTML.index('id="autoPayProxySelect"')
        self.assertIn("用注册代理", HTML[idx:idx + 220])

    def test_loads_pool_and_keeps_disabled(self):
        """停用只影响注册选池，支付仍可手动指定，所以不能过滤掉。"""
        idx = HTML.index("async function _autoPayLoadProxies")
        body = HTML[idx:idx + 1600]
        self.assertIn("/api/proxy-pool", body)
        self.assertIn("filter(p => p.valid)", body)
        self.assertNotIn("p.enabled && p.valid", body)

    def test_marks_disabled_entries(self):
        """能选不等于该默认选，要让人看出这个节点注册时是停用的。"""
        idx = HTML.index("async function _autoPayLoadProxies")
        body = HTML[idx:idx + 1600]
        self.assertIn("注册已停用", body)

    def test_groups_by_country(self):
        idx = HTML.index("async function _autoPayLoadProxies")
        body = HTML[idx:idx + 1400]
        self.assertIn("optgroup", body)
        self.assertIn("country", body)

    def test_warns_when_country_differs_from_billing(self):
        idx = HTML.index("function _autoPayProxyHintSync")
        body = HTML[idx:idx + 800]
        self.assertIn("国家不一致", body)

    def test_sends_name_on_submit(self):
        """提交时用固化后的值：下拉在会话开始后会被隐藏。"""
        self.assertIn("pay_proxy_name: _autoPayUsedProxyName", HTML)
        self.assertIn("_autoPayUsedProxyName = $('#autoPayProxySelect').value", HTML)

    def test_hint_refreshes_on_address_change(self):
        idx = HTML.index("addrSel.onchange")
        self.assertIn("_autoPayProxyHintSync", HTML[idx:idx + 500])


if __name__ == "__main__":
    unittest.main(verbosity=2)
