# -*- coding: utf-8 -*-
"""支付弹窗默认选中上次用过的卡。

配置里的 VCC_CARD_ID 是全局兜底，换卡后每次都要手点一遍。实际用过的那张
才是更合理的默认值。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402

_KEY = "pay_last_card_id"


class LastCardMetaTest(unittest.TestCase):
    def test_roundtrip(self):
        store = {}
        with patch.object(db, "set_meta", side_effect=lambda k, v: store.__setitem__(k, v)), \
             patch.object(db, "get_meta", side_effect=store.get):
            db.set_meta(_KEY, "card-abc")
            self.assertEqual(db.get_meta(_KEY), "card-abc")

    def test_missing_returns_none(self):
        with patch.object(db, "get_meta", return_value=None):
            self.assertIsNone(db.get_meta(_KEY))


class DefaultsPreferenceTest(unittest.TestCase):
    """card_id 的优先级：上次用过的 > 配置兜底。"""

    @staticmethod
    def _resolve(last: str, cfg_card: str) -> str:
        return last or cfg_card

    def test_last_card_wins(self):
        self.assertEqual(self._resolve("last-1", "cfg-1"), "last-1")

    def test_falls_back_to_config(self):
        self.assertEqual(self._resolve("", "cfg-1"), "cfg-1")

    def test_both_empty(self):
        self.assertEqual(self._resolve("", ""), "")


class SourceWiringTest(unittest.TestCase):
    def _app_src(self) -> str:
        return (Path(__file__).resolve().parents[1]
                / "webui" / "app.py").read_text(encoding="utf-8")

    def test_records_card_after_session_starts(self):
        """必须在会话真正起来之后才记，避免把失败的选择记成偏好。"""
        src = self._app_src()
        start = src.index("start_session(account_id=")
        record = src.index('db.set_meta("pay_last_card_id"')
        self.assertLess(start, record)

    def test_defaults_prefers_last_card(self):
        src = self._app_src()
        seg = src[src.index("def api_pay_defaults"):]
        seg = seg[:seg.index("@app.get(\"/api/pay/active\")")]
        self.assertIn('db.get_meta("pay_last_card_id")', seg)
        self.assertIn('last_card or (cfg.VCC_CARD_ID or "")', seg)

    def test_recording_failure_does_not_break_payment(self):
        """记偏好失败不能影响已经发起的支付。"""
        src = self._app_src()
        seg = src[src.index('db.set_meta("pay_last_card_id"') - 400:]
        seg = seg[:seg.index("return jsonify")]
        self.assertIn("except Exception", seg)

    def test_frontend_skips_disabled_card(self):
        html = (Path(__file__).resolve().parents[1]
                / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        seg = html[html.index("// 选卡优先级"):]
        seg = seg[:seg.index("await _autoPayCheckFunds")]
        self.assertIn("!opt.disabled", seg,
                      "没排除不可用的卡，会默认选中一张提交不了的卡")


if __name__ == "__main__":
    unittest.main()
