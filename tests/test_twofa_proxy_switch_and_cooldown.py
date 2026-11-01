import unittest
from unittest.mock import MagicMock, patch

from core import account_export, twofa_service
from core.account_export import _trigger_reauth_with_retry


def resp(status=403, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    return r


def challenge_error():
    exc = Exception("HTTP Error 403: ")
    exc.response = resp(headers={"cf-mitigated": "challenge", "cf-ray": "r-SJC"})
    return exc


def business_error():
    exc = Exception("HTTP Error 403: ")
    exc.response = resp(headers={"content-type": "application/json"})
    return exc


class ProxySwitchOnChallengeTests(unittest.TestCase):
    def setUp(self):
        p = patch("core.account_export.time.sleep")
        p.start()
        self.addCleanup(p.stop)
        c = patch.object(account_export, "_clear_twofa_session_circuit")
        c.start()
        self.addCleanup(c.stop)

    def test_challenge_invokes_callback_and_uses_new_session(self):
        first, second = MagicMock(name="s1"), MagicMock(name="s2")
        seen = []

        def trigger(sess, email):
            seen.append(sess)
            if len(seen) == 1:
                raise challenge_error()
            return "https://auth.ok/url"

        with patch.object(account_export, "_trigger_reauth", side_effect=trigger):
            out = _trigger_reauth_with_retry(
                first, "u@e.test", on_challenge=lambda attempt: second)
        self.assertEqual(out, "https://auth.ok/url")
        # 第二次必须用换来的会话，而不是原来那个被质询的。
        self.assertIs(seen[0], first)
        self.assertIs(seen[1], second)

    def test_callback_not_called_for_business_403(self):
        calls = []
        with patch.object(account_export, "_trigger_reauth", side_effect=business_error()):
            with self.assertRaises(Exception):
                _trigger_reauth_with_retry(
                    MagicMock(), "u@e.test",
                    on_challenge=lambda attempt: calls.append(attempt))
        self.assertEqual(calls, [])

    def test_callback_returning_none_keeps_original_session(self):
        original = MagicMock(name="orig")
        seen = []

        def trigger(sess, email):
            seen.append(sess)
            raise challenge_error()

        with patch.object(account_export, "_trigger_reauth", side_effect=trigger):
            with self.assertRaises(Exception):
                _trigger_reauth_with_retry(
                    original, "u@e.test", on_challenge=lambda attempt: None)
        self.assertTrue(all(s is original for s in seen))

    def test_callback_failure_does_not_abort_retry(self):
        def boom(attempt):
            raise RuntimeError("pool empty")

        with patch.object(account_export, "_trigger_reauth",
                          side_effect=challenge_error()) as tr:
            with self.assertRaises(Exception):
                _trigger_reauth_with_retry(MagicMock(), "u@e.test", on_challenge=boom)
        # 回调炸了也要把 3 次重试跑完，不能提前放弃。
        self.assertEqual(tr.call_count, 3)

    def test_no_callback_preserves_old_behaviour(self):
        with patch.object(account_export, "_trigger_reauth",
                          side_effect=challenge_error()) as tr:
            with self.assertRaises(Exception):
                _trigger_reauth_with_retry(MagicMock(), "u@e.test")
        self.assertEqual(tr.call_count, 3)


class RegistrationCooldownTests(unittest.TestCase):
    def _run(self, trigger, delay):
        slept = []
        with patch.object(twofa_service.time, "sleep", side_effect=slept.append), \
             patch.object(twofa_service._twofa_cfg, "TWOFA_REGISTRATION_DELAY", delay,
                          create=True), \
             patch.object(twofa_service.db, "mark_account_totp_setup_running",
                          return_value=False):
            twofa_service._run_twofa(
                account_id=1, email="u@e.test", access_token="t",
                proxy=None, trigger=trigger)
        return slept

    def test_registration_auto_sleeps(self):
        self.assertEqual(self._run("registration_auto", 180.0), [180.0])

    def test_manual_triggers_do_not_sleep(self):
        self.assertEqual(self._run("manual", 180.0), [])
        self.assertEqual(self._run("manual_bulk", 180.0), [])

    def test_zero_delay_disables_cooldown(self):
        self.assertEqual(self._run("registration_auto", 0), [])

    def test_delay_is_clamped(self):
        self.assertEqual(self._run("registration_auto", 99999), [1800.0])

    def test_cooldown_happens_before_taking_worker_slot(self):
        """冷却必须在占名额之前，否则几个延迟任务会把队列堵死。"""
        order = []
        with patch.object(twofa_service.time, "sleep",
                          side_effect=lambda d: order.append("sleep")), \
             patch.object(twofa_service._twofa_cfg, "TWOFA_REGISTRATION_DELAY", 60.0,
                          create=True), \
             patch.object(twofa_service.db, "mark_account_totp_setup_running",
                          side_effect=lambda *a, **k: order.append("slot") or False):
            twofa_service._run_twofa(
                account_id=1, email="u@e.test", access_token="t",
                proxy=None, trigger="registration_auto")
        self.assertEqual(order, ["sleep", "slot"])


if __name__ == "__main__":
    unittest.main()
