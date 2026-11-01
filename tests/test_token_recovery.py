# -*- coding: utf-8 -*-
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from core import cloakbrowser_registration as cbr
from core import roxy_registration as rr

_SESSION = {"accessToken": "at-recovered", "user": {"id": "u1"}, "account": {"planType": "free"}}


class RecoverHelperTests(unittest.TestCase):
    """账号已建成但没拿到 token 时，改用重新登录补取。"""

    def test_returns_session_from_fresh_login(self):
        live = {"ok": True, "status": "live", "access_token": "at-recovered", "session": _SESSION}
        with patch("core.account_liveness.check_account_liveness", return_value=live) as check, \
             patch.object(rr, "resolve_email_source", return_value="outlook"):
            got = rr.recover_session_after_token_failure("a@b.com", "http://p:1", RuntimeError("callback 失败"))

        self.assertEqual(got["accessToken"], "at-recovered")
        self.assertEqual(check.call_args[0], ("a@b.com", "http://p:1"))
        # 不能清掉注册日志，补救过程要接在同一份日志后面。
        self.assertFalse(check.call_args[1]["clear_log"])
        self.assertEqual(check.call_args[1]["email_source"], "outlook")

    def test_login_failure_reraises_the_original_reason(self):
        original = RuntimeError("等待 accessToken 超时")
        with patch("core.account_liveness.check_account_liveness", side_effect=RuntimeError("登录超时")), \
             patch.object(rr, "resolve_email_source", return_value="outlook"):
            with self.assertRaises(RuntimeError) as ctx:
                rr.recover_session_after_token_failure("a@b.com", None, original)
        self.assertIs(ctx.exception, original, "必须抛原始失败原因，便于定位")

    def test_dead_account_result_reraises_original_reason(self):
        original = RuntimeError("等待 accessToken 超时")
        dead = {"ok": False, "status": "deactivated", "error": "账号已废弃", "session": {}}
        with patch("core.account_liveness.check_account_liveness", return_value=dead), \
             patch.object(rr, "resolve_email_source", return_value="outlook"):
            with self.assertRaises(RuntimeError) as ctx:
                rr.recover_session_after_token_failure("a@b.com", None, original)
        self.assertIs(ctx.exception, original)

    def test_ok_without_token_is_treated_as_failure(self):
        original = RuntimeError("等待 accessToken 超时")
        empty = {"ok": True, "status": "live", "session": {}}
        with patch("core.account_liveness.check_account_liveness", return_value=empty), \
             patch.object(rr, "resolve_email_source", return_value="outlook"):
            with self.assertRaises(RuntimeError) as ctx:
                rr.recover_session_after_token_failure("a@b.com", None, original)
        self.assertIs(ctx.exception, original)


class CloakRegistrationWiringTests(unittest.TestCase):
    """run_cloak_registration 里补救的触发条件：只在账号确实建成后才补。"""

    def _run(self, *, token_error=None, profile_submitted=True, recover=None):
        opened = MagicMock()
        opened.profile_id = "p1"
        opened.raw = {"proxy_pool_target": "http://pool-proxy:8080"}
        driver = MagicMock()
        saved = MagicMock(return_value=77)
        recover_mock = MagicMock(**({"side_effect": recover} if isinstance(recover, BaseException) else {"return_value": recover}))

        with ExitStack() as stack:
            p = lambda name, **kw: stack.enter_context(patch.object(cbr, name, **kw))
            p("build_cloak_driver", return_value=(driver, opened))
            p("PlaywrightTrafficTracker", return_value=MagicMock())
            p("BrowserDataSaver", return_value=MagicMock())
            p("merge_proxy_transport_traffic", side_effect=lambda t, s, label=None: t)
            for noop in ("post_register_dwell", "human_delay", "_check_manual_stop", "_maybe_accept",
                         "_clear_otp_inputs", "_type_otp", "_click_continue"):
                p(noop, return_value=None)
            p("_submit_email_and_wait_next", return_value="otp")
            p("_fill_password_page_if_present", return_value="pw123")
            p("wait_for_otp", return_value="123456")
            p("_wait_after_email_otp_submit", return_value="accepted")
            p("_complete_profile_page", return_value=profile_submitted)
            p("resolve_email_source", return_value="outlook")
            p("save_account_data", new=saved)
            p("recover_session_after_token_failure", new=recover_mock)
            fetch = stack.enter_context(patch.object(
                cbr, "_fetch_chatgpt_session",
                **({"side_effect": token_error} if token_error else {"return_value": _SESSION})))
            stack.enter_context(patch.object(cbr._twofa_cfg, "ENABLE_2FA", False))
            stack.enter_context(patch.object(cbr._cfg, "ENABLE_CODEX_AUTO", False, create=True))
            stack.enter_context(patch.object(cbr._cfg, "CLOAK_KEEP_BROWSER_OPEN", False))
            result = cbr.run_cloak_registration("a@b.com", "N", "2000-01-01", proxy=None)
        return result, recover_mock, saved, fetch

    def test_no_recovery_when_token_was_obtained(self):
        result, recover, saved, _ = self._run()
        recover.assert_not_called()
        saved.assert_called_once()
        self.assertEqual(saved.call_args[1]["access_token"], "at-recovered")

    def test_recovery_runs_after_profile_submitted_and_account_is_saved(self):
        err = RuntimeError("等待 /api/auth/session accessToken 超时")
        result, recover, saved, _ = self._run(token_error=err, recover=_SESSION)

        recover.assert_called_once()
        self.assertEqual(recover.call_args[0][0], "a@b.com")
        # 补救必须沿用注册时的出口，否则换 IP 登录更容易被拦。
        self.assertEqual(recover.call_args[0][1], "http://pool-proxy:8080")
        self.assertIs(recover.call_args[0][2], err)
        saved.assert_called_once()
        self.assertEqual(saved.call_args[1]["access_token"], "at-recovered")
        self.assertTrue(result["success"])

    def test_no_recovery_when_account_was_never_created(self):
        err = RuntimeError("邮箱提交后未进入密码页/验证码页")
        result, recover, saved, _ = self._run(token_error=err, profile_submitted=False)

        recover.assert_not_called()
        saved.assert_not_called()
        self.assertFalse(result["success"])
        self.assertIn("邮箱提交后未进入", result["error"])

    def test_failed_recovery_keeps_the_job_failed(self):
        err = RuntimeError("等待 /api/auth/session accessToken 超时")
        result, recover, saved, _ = self._run(token_error=err, recover=err)

        recover.assert_called_once()
        saved.assert_not_called()
        self.assertFalse(result["success"])
        self.assertIn("accessToken", result["error"])


class EmailReleaseTests(unittest.TestCase):
    """补救失败时邮箱必须按"已消耗"回收，不能放回池子再被抽到。"""

    def test_created_account_marks_email_failed_not_available(self):
        import inspect
        src = inspect.getsource(cbr.run_cloak_registration)
        self.assertIn('status="failed" if create_acknowledged else "available"', src)


if __name__ == "__main__":
    unittest.main()
