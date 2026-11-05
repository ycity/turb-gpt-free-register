# -*- coding: utf-8 -*-
"""DOM 直写不能用严格相等判断成败。

事故：为了解决复用长链时字段被锁死，_fill_via_dom 里加了
`const ok = el.value === val` 回读确认。但 Stripe 会给卡号自动插空格
（5378727141745290 → 5378 7271 4174 5290），严格相等永远为 false，
于是写入明明成功却被判成失败，支付在 card_number 这一步直接中断，
报出误导性的「指针被遮挡且 DOM 写入未成功」——实测字段根本没被遮挡。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "core" / "vcc_checkout_pay.py").read_text(encoding="utf-8")


class FormattingInput:
    """模拟 Stripe：写进去的数字会被重新格式化成带空格的卡号。"""

    def __init__(self, formats=True):
        self._raw = ""
        self.formats = formats
        self.disabled = False
        self.readOnly = False

    def set(self, v):
        digits = "".join(ch for ch in str(v) if ch.isdigit())
        if self.formats and digits:
            self._raw = " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))
        else:
            self._raw = str(v)

    @property
    def value(self):
        return self._raw


class FakePage:
    """够用的假页面：evaluate 里跑真实 JS 语义的 Python 等价物。"""

    def __init__(self, el):
        self.el = el

    def evaluate(self, script, arg=None):
        # 只还原脚本的判定语义：写入 → 读回 → 按脚本的方式判断成败
        _sel, val = arg
        self.el.set(val)
        if "el.value === val" in script:
            return self.el.value == val          # 旧行为
        return len(str(self.el.value or "")) > 0  # 新行为

    def input_value(self, selector, timeout=0):
        return self.el.value


class DomWriteVerdictTest(unittest.TestCase):
    def test_formatted_card_number_counts_as_success(self):
        from core.vcc_checkout_pay import _fill_via_dom
        el = FormattingInput(formats=True)
        ok = _fill_via_dom(FakePage(el), "#cardNumber", "5378727141745290")
        self.assertTrue(ok, "Stripe 插空格后仍应判成功")
        self.assertEqual(el.value, "5378 7271 4174 5290")

    def test_plain_field_still_works(self):
        from core.vcc_checkout_pay import _fill_via_dom
        el = FormattingInput(formats=False)
        ok = _fill_via_dom(FakePage(el), "#billingLocality", "Juneau")
        self.assertTrue(ok)
        self.assertEqual(el.value, "Juneau")

    def test_empty_write_is_failure(self):
        """真写不进去时仍要返回 False，不能一律报成功。"""
        from core.vcc_checkout_pay import _fill_via_dom
        el = FormattingInput(formats=False)
        ok = _fill_via_dom(FakePage(el), "#x", "")
        self.assertFalse(ok)


class SourceGuardTest(unittest.TestCase):
    def test_no_strict_equality_check(self):
        idx = SRC.index("def _fill_via_dom")
        body = SRC[idx:SRC.index("def _digits")]
        self.assertNotIn("el.value === val", body,
                         "严格相等会被 Stripe 的格式化打败")

    def test_checks_non_empty_instead(self):
        idx = SRC.index("def _fill_via_dom")
        body = SRC[idx:SRC.index("def _digits")]
        self.assertIn("length > 0", body)

    def test_still_restores_lock_state(self):
        """放宽校验不能把解锁后还原那段弄丢。"""
        idx = SRC.index("def _fill_via_dom")
        body = SRC[idx:SRC.index("def _digits")]
        self.assertIn("if (wasDisabled) el.disabled = true", body)
        self.assertIn("if (wasReadOnly) el.readOnly = true", body)

    def test_precise_check_still_done_by_caller(self):
        """卡号差一位就作废，精确校验必须还在，只是挪到调用方。"""
        self.assertIn("_value_matches(page, selector, value)", SRC)

    def test_error_message_states_facts_only(self):
        """实测字段没被遮挡，错误信息不该把原因说死。"""
        self.assertNotIn("指针被遮挡且 DOM 写入未成功", SRC)
        self.assertIn("DOM 写入未生效", SRC)

    def test_error_message_includes_actual_value(self):
        idx = SRC.index("DOM 写入未生效")
        window = SRC[max(0, idx - 400):idx + 200]
        self.assertIn("input_value", window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
