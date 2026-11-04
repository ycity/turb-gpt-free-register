# -*- coding: utf-8 -*-
"""复用旧长链时账单地址被 Stripe 锁死的适配。

场景：上一个 team 长链已经填过 email / 地址，再复用同一条长链时 Stripe 会
把这些字段收成只读摘要并加 disabled+readonly。email 锁了无所谓（本来就是
本账号的），但地址锁死会导致支付提交不了，必须能覆盖。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class FakeLocator:
    def __init__(self, visible=True):
        self._visible = visible
        self.clicked = False

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return self._visible

    def click(self, timeout=0):
        self.clicked = True

    def scroll_into_view_if_needed(self, timeout=0):
        pass


class FakePage:
    """够用的假页面：记录 evaluate 脚本，按 selector 返回可见性。"""

    def __init__(self, *, line1_visible=True, manual_link=False):
        self.scripts = []
        self.line1_visible = line1_visible
        self.manual_link = manual_link
        self.waited = []
        self._text_locator = FakeLocator(visible=manual_link)

    def locator(self, sel):
        from core.vcc_checkout_pay import SEL_LINE1
        return FakeLocator(visible=(sel == SEL_LINE1 and self.line1_visible))

    def get_by_text(self, name, exact=False):
        return self._text_locator

    def evaluate(self, script, arg=None):
        self.scripts.append((script, arg))
        if "enter address manually" in script.lower():
            return self.manual_link
        return True

    def wait_for_selector(self, sel, timeout=0):
        self.waited.append(sel)


class DomWriteUnlocksFieldTest(unittest.TestCase):
    """_fill_via_dom 必须先解锁再写，写完还原。"""

    def _script(self):
        page = FakePage()
        from core.vcc_checkout_pay import _fill_via_dom
        _fill_via_dom(page, "#billingAddressLine1", "1 Main St")
        return page.scripts[0][0]

    def test_clears_disabled_before_writing(self):
        s = self._script()
        self.assertIn("el.disabled = false", s)

    def test_clears_readonly_before_writing(self):
        s = self._script()
        self.assertIn("el.readOnly = false", s)

    def test_restores_original_lock_state(self):
        s = self._script()
        self.assertIn("if (wasDisabled) el.disabled = true", s)
        self.assertIn("if (wasReadOnly) el.readOnly = true", s)

    def test_verifies_the_value_actually_stuck(self):
        """React 可能回滚写入，必须回读确认而不是无脑返回 true。"""
        s = self._script()
        self.assertIn("el.value === val", s)

    def test_passes_selector_and_value_as_args(self):
        page = FakePage()
        from core.vcc_checkout_pay import _fill_via_dom
        _fill_via_dom(page, "#billingLocality", "Juneau")
        self.assertEqual(page.scripts[0][1], ["#billingLocality", "Juneau"])


class SelectUnlocksTest(unittest.TestCase):
    def test_select_via_dom_also_unlocks(self):
        page = FakePage()
        from core.vcc_checkout_pay import _select_via_dom
        _select_via_dom(page, "select#billingCountry", "US")
        s = page.scripts[0][0]
        self.assertIn("el.disabled = false", s)
        self.assertIn("if (wasDisabled) el.disabled = true", s)

    def test_restores_lock_even_when_option_missing(self):
        """找不到选项也要还原，不能把锁摘了就走。"""
        page = FakePage()
        from core.vcc_checkout_pay import _select_via_dom
        _select_via_dom(page, "select#billingCountry", "US")
        s = page.scripts[0][0]
        self.assertIn("if (hit < 0) { if (wasDisabled) el.disabled = true; return false; }", s)


class ExpandManualAddressTest(unittest.TestCase):
    def test_noop_when_form_already_visible(self):
        from core.vcc_checkout_pay import _expand_manual_address
        page = FakePage(line1_visible=True)
        self.assertFalse(_expand_manual_address(page))
        self.assertEqual(page.waited, [])

    def test_clicks_link_when_address_is_collapsed(self):
        from core.vcc_checkout_pay import _expand_manual_address
        page = FakePage(line1_visible=False, manual_link=True)
        self.assertTrue(_expand_manual_address(page))
        self.assertTrue(page._text_locator.clicked)

    def test_waits_for_line1_after_expanding(self):
        from core.vcc_checkout_pay import SEL_LINE1, _expand_manual_address
        page = FakePage(line1_visible=False, manual_link=True)
        _expand_manual_address(page)
        self.assertIn(SEL_LINE1, page.waited)

    def test_returns_false_when_nothing_to_expand(self):
        from core.vcc_checkout_pay import _expand_manual_address
        page = FakePage(line1_visible=False, manual_link=False)
        self.assertFalse(_expand_manual_address(page))


class FillFlowWiringTest(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "core"
                    / "vcc_checkout_pay.py").read_text(encoding="utf-8")

    def test_expand_runs_before_country_select(self):
        expand = self.src.index("_expand_manual_address(page)")
        country = self.src.index("label=\"billing_country\"")
        self.assertLess(expand, country)

    def test_expand_runs_before_line1_fill(self):
        expand = self.src.index("_expand_manual_address(page)")
        line1 = self.src.index("label=\"billing_line1\"")
        self.assertLess(expand, line1)

    def test_email_is_not_fatal(self):
        """email 锁死不该中断支付，地址才是必须的。"""
        idx = self.src.index('label="email"')
        window = self.src[idx:idx + 120]
        self.assertIn("required=False", window)

    def test_address_fields_stay_required(self):
        for label in ("billing_line1", "billing_city"):
            idx = self.src.index(f'label="{label}"')
            window = self.src[idx:idx + 120]
            self.assertNotIn("required=False", window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
