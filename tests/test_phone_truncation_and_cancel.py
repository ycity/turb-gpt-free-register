# -*- coding: utf-8 -*-
"""两个实际踩到的坑。

一、手机号可见输入框有 maxLength，填完整 E.164 会被截掉尾数。
    2026-11-05 实测：巴西号 +5561920088836（13 位）填进去只剩
    "+55 61 92008 883"（12 位），校验不符就换号，连续 10 次全挂在这一步，
    一条短信都没发出去。泰国号 11 位没超限，一次就过。

二、cancel() 的「等满 2 分钟再取消」跑在 daemon 线程里，主进程一退出线程
    就被杀，号卡在 STATUS_WAIT_CODE 不退款。同一次事故里 10 个号 $0.66
    全部悬空，只能事后手动 setStatus=8 找回。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
OAUTH = (ROOT / "core" / "roxy_codex_oauth.py").read_text(encoding="utf-8")
SMS = (ROOT / "core" / "sms_provider.py").read_text(encoding="utf-8")


class PhoneTruncationGuardTest(unittest.TestCase):
    """写入侧要先看 maxLength，放不下就退回国内号段。"""

    def test_reads_max_length(self):
        self.assertIn("phoneInput.maxLength", OAUTH)

    def test_falls_back_to_national_number(self):
        self.assertIn("truncationGuard", OAUTH)
        idx = OAUTH.index("truncationGuard")
        block = OAUTH[idx:idx + 1200]
        self.assertIn("visibleValue.length > maxLen", block)

    def test_uses_real_country_codes_not_blind_length(self):
        """失败那次 dialCode 是空的，必须有兜底。

        但不能按长度盲试：+5561920088836 砍 3 位会得到 1920088836，
        把区号的 6 也吃掉了。必须按真实国家码表切。
        """
        idx = OAUTH.index("truncationGuard")
        block = OAUTH[idx:idx + 2500]
        self.assertIn("const CC = [", block)
        self.assertIn("digits.startsWith(c)", block)
        self.assertNotIn("[3, 2, 1]", block, "盲试长度会切错区号")

    def test_guard_reported_back(self):
        """诊断信息要带回来，否则下次又得从换号 10 次查起。"""
        self.assertIn("maxLength: maxLen", OAUTH)
        self.assertIn("truncationGuard,", OAUTH)

    def test_error_explains_truncation(self):
        idx = OAUTH.index("手机号可见输入框校验失败")
        window = OAUTH[max(0, idx - 700):idx + 200]
        self.assertIn("可见框被截断", window)
        self.assertIn("maxLength", window)

    def test_hidden_field_still_full_e164(self):
        """国家码由 select 承担，隐藏字段不能跟着截断。"""
        self.assertIn("hiddenPhoneNumberInput.value = e164", OAUTH)


class TruncationLogicTest(unittest.TestCase):
    """把那段 JS 的判断逻辑用 Python 复算，确认真实号码能过。"""

    # 和 JS 里那张表保持一致的子集，够覆盖测试用例
    CC = ["886", "880", "852", "598", "595", "593", "591", "507", "506",
          "98", "95", "94", "93", "92", "91", "90", "86", "84", "82", "81",
          "66", "65", "64", "63", "62", "61", "60", "58", "57", "56", "55",
          "54", "53", "52", "51", "49", "48", "47", "46", "45", "44", "43",
          "7", "1"]

    @classmethod
    def pick(cls, e164, dial_code, max_len):
        digits = re.sub(r"\D", "", e164)
        visible = e164
        if dial_code and digits.startswith(dial_code) and len(digits) > len(dial_code) + 3:
            visible = digits[len(dial_code):]
        if max_len > 0 and len(visible) > max_len:
            national = ""
            if dial_code and digits.startswith(dial_code):
                national = digits[len(dial_code):]
            else:
                cc = next((c for c in cls.CC if digits.startswith(c)), "")
                if cc:
                    national = digits[len(cc):]
            if national and len(national) <= max_len:
                visible = national
        return visible

    def test_brazil_13_digits_fits_after_guard(self):
        """就是这个号炸掉了 #62。"""
        out = self.pick("+5561920088836", "", 13)
        self.assertEqual(out, "61920088836", "只该去掉国家码 55")
        self.assertLessEqual(len(out), 13)

    def test_guard_matches_dialcode_path(self):
        """有没有 dialCode，结果应该一致——否则说明国家码切错了。"""
        self.assertEqual(self.pick("+5561920088836", "", 13),
                         self.pick("+5561920088836", "55", 13))

    def test_thailand_unaffected(self):
        """泰国号本来就能放下，不该被改写。"""
        out = self.pick("+66940847285", "", 15)
        self.assertEqual(out, "+66940847285")

    def test_uses_dialcode_when_available(self):
        out = self.pick("+5561920088836", "55", 13)
        self.assertEqual(out, "61920088836")

    def test_no_maxlength_keeps_e164(self):
        out = self.pick("+5561920088836", "", 0)
        self.assertEqual(out, "+5561920088836")


class PendingCancelTest(unittest.TestCase):
    """取消线程不能随主进程一起消失。"""

    def test_registry_exists(self):
        self.assertIn("_PENDING_CANCELS", SMS)

    def test_atexit_waits(self):
        self.assertIn("atexit.register(_wait_pending_cancels)", SMS)
        self.assertIn("import atexit", SMS)

    def test_spawn_helper_used_everywhere(self):
        """两处派线程都要登记，漏一处就还会悬空。"""
        self.assertEqual(SMS.count("_spawn_cancel(activation_id)"), 2)
        idx = SMS.index("def cancel")
        body = SMS[idx:]
        self.assertNotIn("threading.Thread(", body,
                         "cancel 里不该再裸起线程")

    def test_thread_removes_itself_when_done(self):
        idx = SMS.index("def _spawn_cancel")
        body = SMS[idx:idx + 700]
        self.assertIn("_PENDING_CANCELS.discard", body)

    def test_warns_when_still_stuck(self):
        idx = SMS.index("def _wait_pending_cancels")
        body = SMS[idx:idx + 900]
        self.assertIn("可能未退款", body)

    def test_incident_documented(self):
        """写清楚为什么要有这套东西，不然下次又被当成多余代码删掉。"""
        idx = SMS.index("_PENDING_CANCELS")
        window = SMS[max(0, idx - 600):idx]
        self.assertIn("daemon", window)
        self.assertIn("STATUS_WAIT_CODE", window)


class WaitHelperBehaviourTest(unittest.TestCase):
    def test_returns_immediately_with_no_threads(self):
        from core.sms_provider import _wait_pending_cancels
        _wait_pending_cancels(timeout=0.1)   # 不该抛，也不该卡住

    def test_joins_live_thread(self):
        import threading
        import time as _t
        from core import sms_provider as sp

        done = []

        def slow():
            _t.sleep(0.3)
            done.append(1)

        t = threading.Thread(target=slow, daemon=True)
        with sp._PENDING_LOCK:
            sp._PENDING_CANCELS.add(t)
        t.start()
        try:
            sp._wait_pending_cancels(timeout=5)
            self.assertEqual(done, [1], "应该等到线程跑完")
        finally:
            with sp._PENDING_LOCK:
                sp._PENDING_CANCELS.discard(t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
