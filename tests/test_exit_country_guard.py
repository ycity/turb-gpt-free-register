# -*- coding: utf-8 -*-
"""出口国家校验：手动起支付前拦住"出口与结算区不一致"。

2026-11-18 事故：#110 用 /api/accounts/pay/start 起支付时没传 pay_proxy，
回落到账号记录的 proxy_used，实测出口在加拿大；而结账页按 EGP（埃及）
结算、卡账单地址在美国。三国错配 -> Stripe 直接 "Payment was not approved"，
不扣款也不报明确错误，还被记成已付款。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def _need_flask():
    """本机（无 flask）跳过；容器内会真跑。"""
    try:
        import flask  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("本机没装 flask，容器内会跑")


class ExitCountryTest(unittest.TestCase):
    def _mismatch(self, got_cc, want_cc="EG"):
        _need_flask()
        from webui.app import _exit_country_mismatch
        with patch("webui.app._exit_country_of", return_value=got_cc):
            return _exit_country_mismatch("http://u:p@1.2.3.4:1", want_cc)

    def test_same_country_passes(self):
        self.assertEqual(self._mismatch("EG"), "")

    def test_different_country_blocked(self):
        msg = self._mismatch("CA")
        self.assertTrue(msg)
        self.assertIn("CA", msg)
        self.assertIn("EG", msg)

    def test_probe_failure_does_not_block(self):
        """探测失败（出口抖动）不该把正常单挡死，交给提交后核验兜住。"""
        self.assertEqual(self._mismatch(""), "")

    def test_no_settlement_country_passes(self):
        self.assertEqual(self._mismatch("CA", want_cc=""), "")

    def test_case_insensitive(self):
        self.assertEqual(self._mismatch("eg"), "")

    def test_message_mentions_payment_not_approved(self):
        """错误文案要能让人看懂根因，而不是只知道"被拒"。"""
        msg = self._mismatch("CA")
        self.assertIn("not approved", msg.lower())


class SettlementCountryTest(unittest.TestCase):
    def test_reads_auto_subscribe_country(self):
        _need_flask()
        from webui.app import _settlement_country
        with patch("config.auto_subscribe.AUTO_SUBSCRIBE_COUNTRY", "EG"):
            self.assertEqual(_settlement_country(), "EG")

    def test_defaults_to_eg_when_missing(self):
        _need_flask()
        from webui.app import _settlement_country
        with patch("config.auto_subscribe.AUTO_SUBSCRIBE_COUNTRY", ""):
            self.assertEqual(_settlement_country(), "EG")


class WiringTest(unittest.TestCase):
    def test_pay_start_calls_mismatch_check(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def api_account_pay_start")
        seg = src[i:i + 12000]
        self.assertIn("_exit_country_mismatch", seg)
        self.assertIn("_settlement_country", seg)

    def test_check_runs_before_actual_start_session(self):
        """校验必须在真正起支付会话之前。

        注意别拿函数顶部的 `from core.vcc_pay_session import start_session`
        当参照点——那是 import，不是调用。
        """
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def api_account_pay_start")
        seg = src[i:i + 12000]
        check_pos = seg.index("_exit_country_mismatch(")
        # 实际调用形如 "sess = start_session("
        call_pos = seg.index("sess = start_session(")
        self.assertLess(check_pos, call_pos)


if __name__ == "__main__":
    unittest.main()
