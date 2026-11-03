# -*- coding: utf-8 -*-
"""表单字段操作不该用页面级的 60 秒超时。

被遮挡的 fill/select 注定失败，代码随后立刻走 DOM 直写。原先这些操作沿用
page.set_default_timeout(VCC_PAGE_TIMEOUT=60s)，表单十几个字段里几个被遮挡
就是好几分钟——用户实测填表要十分钟以上。
"""
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vcc_checkout_pay as vp  # noqa: E402

SRC = Path(vp.__file__).read_text(encoding="utf-8")


class FieldTimeoutTest(unittest.TestCase):
    def test_default_is_short(self):
        from config import vcc as cfg
        self.assertLessEqual(cfg.VCC_FIELD_TIMEOUT, 15.0)
        self.assertLess(cfg.VCC_FIELD_TIMEOUT, cfg.VCC_PAGE_TIMEOUT,
                        "字段超时必须远小于页面超时，否则没有意义")

    def test_helper_reads_config(self):
        with patch("config.vcc.VCC_FIELD_TIMEOUT", 4.0):
            self.assertEqual(vp._field_timeout_ms(), 4000)

    def test_helper_survives_bad_config(self):
        with patch("config.vcc.VCC_FIELD_TIMEOUT", "oops"):
            self.assertEqual(vp._field_timeout_ms(), 6000)
        with patch("config.vcc.VCC_FIELD_TIMEOUT", 0):
            self.assertGreaterEqual(vp._field_timeout_ms(), 1000)


class AppliedToOperationsTest(unittest.TestCase):
    def test_fill_passes_timeout(self):
        page = MagicMock()
        page.input_value.return_value = "4111111111111111"
        with patch("config.vcc.VCC_FIELD_TIMEOUT", 5.0):
            vp._fill(page, "#num", "4111111111111111",
                     label="卡号", filled={}, exact=True)
        self.assertEqual(page.fill.call_args.kwargs.get("timeout"), 5000)

    def test_select_passes_timeout(self):
        page = MagicMock()
        with patch("config.vcc.VCC_FIELD_TIMEOUT", 5.0):
            vp._select(page, "#st", "OR", label="州", filled={})
        self.assertEqual(page.select_option.call_args.kwargs.get("timeout"), 5000)

    def test_check_passes_timeout(self):
        page = MagicMock()
        page.is_checked.return_value = True
        with patch("config.vcc.VCC_FIELD_TIMEOUT", 5.0):
            vp._check(page, "#tos", label="条款", filled={}, required=True)
        self.assertEqual(page.is_checked.call_args.kwargs.get("timeout"), 5000)

    def test_blocked_field_still_falls_back_to_dom(self):
        """缩短超时不能牺牲兜底：超时后仍要走 DOM 直写。"""
        page = MagicMock()
        page.fill.side_effect = RuntimeError("timeout")
        page.evaluate.return_value = True
        page.input_value.return_value = "abc"
        filled = {}
        with patch("config.vcc.VCC_FIELD_TIMEOUT", 1.0):
            vp._fill(page, "#x", "abc", label="字段", filled=filled)
        self.assertTrue(filled["字段"])
        page.evaluate.assert_called()

    def test_submit_click_keeps_long_timeout(self):
        """真实提交不能快速放弃，那里不该用字段超时。"""
        seg = SRC[SRC.index('result.stage = "submit"'):]
        seg = seg[:seg.index("result.submitted = True")]
        self.assertNotIn("_field_timeout_ms", seg)


class SessionTimeoutTest(unittest.TestCase):
    def test_session_window_extended(self):
        from config import vcc as cfg
        self.assertGreaterEqual(cfg.VCC_SESSION_TIMEOUT, 1800.0,
                                "等人确认和等 3DS 共用这个上限，15 分钟太短")

    def test_still_overridable_from_env(self):
        src = Path("config/vcc.py").read_text(encoding="utf-8")
        seg = src[src.index("apply_env_overrides"):]
        self.assertIn("'VCC_SESSION_TIMEOUT': 'float'", seg)
        self.assertIn("'VCC_FIELD_TIMEOUT': 'float'", seg)


if __name__ == "__main__":
    unittest.main()
