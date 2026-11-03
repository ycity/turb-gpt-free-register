# -*- coding: utf-8 -*-
"""账单州/省校验：下拉 value 是代码，label 是全称，两者都要认。

线上事故（2026-11-03 05:35，账号 17）：配置里写 Oregon，_select 按 label
成功选中，但校验只读 value 拿到 OR，字符串不等就报"未选中"并中止。那条长链
已经消耗了优惠码 K6M3PPZELXW7GWAH，却因为这个误判白白作废。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vcc_checkout_pay as vp  # noqa: E402


def _playwright_chromium():
    """返回可用的 chromium 启动参数；不可用则返回 None。"""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return None
    import os
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return {}
    shell = Path(local) / ("ms-playwright/chromium_headless_shell-1243/"
                           "chrome-headless-shell-win64/chrome-headless-shell.exe")
    return {"executable_path": str(shell)} if shell.exists() else {}


class SelectedStateTest(unittest.TestCase):
    def test_reads_both_value_and_label(self):
        page = MagicMock()
        page.evaluate.return_value = {"value": "OR", "label": "Oregon"}
        got = vp._selected_state(page)
        self.assertEqual(got, {"value": "OR", "label": "Oregon"})

    def test_missing_element_is_not_fatal(self):
        page = MagicMock()
        page.evaluate.return_value = None
        self.assertEqual(vp._selected_state(page), {"value": "", "label": ""})

    def test_evaluate_failure_is_not_fatal(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("ctx destroyed")
        self.assertEqual(vp._selected_state(page), {"value": "", "label": ""})


class StateMatchLogicTest(unittest.TestCase):
    """复刻 fill_checkout 里的比对规则，确保两种写法都放行。"""

    @staticmethod
    def _accepts(want: str, value: str, label: str) -> bool:
        w = want.strip().casefold()
        return w in (value.strip().casefold(), label.strip().casefold())

    def test_full_name_config_matches_code_value(self):
        # 这正是线上误判的那组
        self.assertTrue(self._accepts("Oregon", "OR", "Oregon"))

    def test_code_config_matches(self):
        self.assertTrue(self._accepts("OR", "OR", "Oregon"))

    def test_alaska_both_ways(self):
        self.assertTrue(self._accepts("AK", "AK", "Alaska"))
        self.assertTrue(self._accepts("Alaska", "AK", "Alaska"))

    def test_case_and_space_insensitive(self):
        self.assertTrue(self._accepts(" oregon ", "OR", "Oregon"))
        self.assertTrue(self._accepts("or", "OR", "Oregon"))

    def test_genuinely_wrong_state_is_rejected(self):
        # 真选错了还是要拦住：州影响税费
        self.assertFalse(self._accepts("Oregon", "CA", "California"))
        self.assertFalse(self._accepts("AK", "OR", "Oregon"))

    def test_empty_selection_is_rejected(self):
        self.assertFalse(self._accepts("Oregon", "", ""))


class FillChecksStateTest(unittest.TestCase):
    def test_fill_accepts_label_selected_state(self):
        """端到端：配置 Oregon、页面 value=OR 时不该抛错。"""
        page = MagicMock()
        page.evaluate.return_value = {"value": "OR", "label": "Oregon"}
        page.input_value.return_value = "OR"

        billing = {"name": "T U", "country": "US", "state": "Oregon",
                   "city": "Portland", "line1": "1 Main St", "line2": "",
                   "postal": "97201"}
        filled: dict = {}

        with patch.object(vp, "_select") as sel:
            # 复刻校验段
            vp._select(page, vp.SEL_STATE, billing["state"],
                       label="billing_state", filled=filled, required=True)
            got = vp._selected_state(page)
            want = billing["state"].strip().casefold()
            ok = want in (got["value"].strip().casefold(),
                          got["label"].strip().casefold())
        self.assertTrue(sel.called)
        self.assertTrue(ok, "按全称选中被误判为失败")

    def test_source_no_longer_compares_value_only(self):
        """锁住修复：不能再退回只比 input_value 的写法。"""
        src = Path(vp.__file__).read_text(encoding="utf-8")
        seg = src[src.index("want_state = billing.get"):]
        seg = seg[:seg.index("_fill(page, SEL_POSTAL")]
        self.assertIn("_selected_state", seg)
        self.assertNotIn("actual.strip().upper() != want_state", seg)


class RealDomStateTest(unittest.TestCase):
    """真实 Chromium：Stripe 州下拉 value 是代码、label 是全称。

    纯 mock 证明不了这一点——bug 的根源就是对 DOM 行为的误解。
    """

    PAGE = """
    <select id="billingAdministrativeArea">
      <option value="">Select</option>
      <option value="AK">Alaska</option>
      <option value="CA">California</option>
      <option value="OR">Oregon</option>
    </select>
    """

    @classmethod
    def setUpClass(cls):
        launch = _playwright_chromium()
        if launch is None:
            raise unittest.SkipTest("未安装 playwright 或缺少 chromium")
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch(**launch)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "_pw", None):
            cls._pw.stop()

    def test_label_selection_yields_code_value(self):
        page = self.browser.new_page()
        try:
            page.set_content(self.PAGE)
            vp._select(page, vp.SEL_STATE, "Oregon",
                       label="billing_state", filled={}, required=True)
            got = vp._selected_state(page)
            self.assertEqual(got["value"], "OR")
            self.assertEqual(got["label"], "Oregon")
            # 旧实现只比这个，必然不等于 "OREGON"
            self.assertEqual(page.input_value(vp.SEL_STATE), "OR")
        finally:
            page.close()

    def test_new_rule_accepts_what_old_rule_rejected(self):
        page = self.browser.new_page()
        try:
            page.set_content(self.PAGE)
            vp._select(page, vp.SEL_STATE, "Oregon",
                       label="billing_state", filled={}, required=True)
            got = vp._selected_state(page)
            want = "oregon"
            self.assertIn(want, (got["value"].casefold(), got["label"].casefold()))
            self.assertNotEqual(page.input_value(vp.SEL_STATE).upper(), "OREGON",
                                "没能复现旧 bug，说明前提判断有误")
        finally:
            page.close()

    def test_wrong_state_still_blocked(self):
        page = self.browser.new_page()
        try:
            page.set_content(self.PAGE)
            page.select_option(vp.SEL_STATE, value="CA")
            got = vp._selected_state(page)
            self.assertNotIn("oregon",
                             (got["value"].casefold(), got["label"].casefold()))
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
