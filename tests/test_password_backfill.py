# -*- coding: utf-8 -*-
"""补密码（忘记密码邮件流）的接口与服务测试。

背景（2026-11-19）：CloakBrowser 注册的账号走一次性验证码分支，从未给
ChatGPT 账号设过密码；查活只能依赖邮箱 OTP，被风控丢弃时就失败（#9）。
补上密码后登录自动优先"密码+TOTP"。协议端点逆向自 auth.openai.com SPA：
password/send-otp → email-otp/validate → password/reset（≥12 位）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402

SEED = [{
    "id": 9101,
    "email": "pwd-test@outlook.com",
    "access_token": "tok-old",
    "email_source": "outlook",
    "extra_json": json.dumps({"user": {"email": "pwd-test@outlook.com"}}),
}]


class _TempDbTestCase(unittest.TestCase):
    """与 test_account_note 相同的临时库隔离方式。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        accounts_path = root / "accounts.json"
        accounts_path.write_text(json.dumps(SEED), encoding="utf-8")
        enter = patch.object(db, "_ACCOUNTS_JSON", accounts_path)
        enter2 = patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy_accounts.json")
        enter3 = patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt")
        enter4 = patch.object(db, "_TOKENS_TXT", root / "tokens.txt")
        enter5 = patch.object(db, "_OUTLOOK_JSON", root / "outlook.json")
        enter6 = patch.object(db, "_GENERIC_API_EMAIL_JSON", root / "api_emails.json")
        enter7 = patch.object(db, "_GENERIC_API_EMAIL_TXT", root / "api_emails.txt")
        enter8 = patch.object(db, "_JOBS_JSON", root / "jobs.json")
        for p in (enter, enter2, enter3, enter4, enter5, enter6, enter7, enter8):
            p.start()
            self.addCleanup(p.stop)
        self.acc_id = 9101
        row = db.get_account(self.acc_id)
        self.assertIsNotNone(row, "种子账号导入失败")


class ResetFlowTest(unittest.TestCase):
    """core.password_reset 的端点与载荷。"""

    def test_module_imports(self):
        from core import password_reset
        self.assertTrue(callable(password_reset.trigger_add_password_reauth))
        self.assertTrue(callable(password_reset.submit_new_password))
        self.assertTrue(callable(password_reset.run_password_backfill))
        self.assertEqual(password_reset.PASSWORD_MIN_LENGTH, 12)

    def test_short_password_rejected_before_any_request(self):
        """OpenAI 要求 ≥12 位；短密码在发起流程前就该被拦下。"""
        from core import password_reset
        with patch("core.account_liveness._login_via_full_web_flow") as login:
            with self.assertRaises(ValueError):
                password_reset.run_password_backfill(
                    "a@b.test", proxy=None, new_password="short", otp_waiter=lambda ts: "123456")
        login.assert_not_called()

    def test_reauth_signin_uses_add_password_action(self):
        """reauth 走 chatgpt.com signin，callbackUrl action=add_password。"""
        from core import password_reset as pr

        session = MagicMock()
        session.get.return_value = MagicMock(status_code=200)
        session.get.return_value.json.return_value = {"csrfToken": "CSRF"}
        signin_resp = MagicMock(status_code=200)
        signin_resp.json.return_value = {"url": "https://auth.openai.com/authorize?x=1"}
        session.post.return_value = signin_resp
        nav = MagicMock(status_code=200)
        nav.url = "https://auth.openai.com/email-verification?y=2"
        session.get.side_effect = [session.get.return_value, nav]

        pr.trigger_add_password_reauth(session, "a@b.test")

        signin_url = session.post.call_args.args[0]
        self.assertIn("https://chatgpt.com/api/auth/signin/openai", signin_url)
        self.assertIn("reauth=password", signin_url)
        body = session.post.call_args.kwargs["data"]
        self.assertIn("action%3Dadd_password", body)

    def test_reauth_rejects_unexpected_landing(self):
        """authorize 没落到 email-verification 说明 reauth 未按预期触发。"""
        from core import password_reset as pr

        session = MagicMock()
        session.get.return_value = MagicMock(status_code=200)
        session.get.return_value.json.return_value = {"csrfToken": "CSRF"}
        signin_resp = MagicMock(status_code=200)
        signin_resp.json.return_value = {"url": "https://auth.openai.com/authorize?x=1"}
        session.post.return_value = signin_resp
        nav = MagicMock(status_code=200)
        nav.url = "https://chatgpt.com/"  # 异常落点
        session.get.side_effect = [session.get.return_value, nav]

        with self.assertRaises(RuntimeError):
            pr.trigger_add_password_reauth(session, "a@b.test")

    def test_submit_posts_password_add_with_sentinel(self):
        """新密码提交到 password/add，并带 password_reset sentinel 头。"""
        from core import password_reset as pr

        session = MagicMock()
        ok_json = MagicMock(status_code=200, text="{}")
        ok_json.json.return_value = {}
        with patch.object(pr, "request_sentinel_token") as req_sentinel, \
             patch.object(pr, "build_sentinel_header", return_value=("S", "SO")) as build, \
             patch.object(pr, "_post_json", return_value=ok_json) as post_json:
            pr.submit_new_password(session, "a@b.test", "Zx9!VeryLongPass1")

        self.assertEqual(req_sentinel.call_args.args[1], "password_reset")
        self.assertEqual(build.call_args.args[2], "password_reset")
        self.assertEqual(
            post_json.call_args.args[1],
            "https://auth.openai.com/api/accounts/password/add")
        self.assertEqual(post_json.call_args.args[2], {"password": "Zx9!VeryLongPass1"})


class DbStateTest(_TempDbTestCase):
    """db 层补密码状态机与密码合并写入。"""

    def test_claim_and_stale_takeover(self):
        self.assertTrue(db.claim_account_password_backfill(self.acc_id, "manual"))
        # 未超时的排队任务再次 claim 被拒。
        self.assertFalse(db.claim_account_password_backfill(self.acc_id, "manual"))
        self.assertEqual(
            db.get_account(self.acc_id)["password_backfill_status"], "queued")
        # 伪造超时后可被重新占用。
        with db._LOCK, db._account_for_update(self.acc_id) as row:
            row["password_backfill_queued_at"] = "2000-01-01T00:00:00"
        self.assertTrue(db.claim_account_password_backfill(self.acc_id, "manual"))

    def test_success_persists_password_and_keeps_extra(self):
        """成功结果要把密码合并进 extra_json，且保留 extra 其它键。"""
        db.claim_account_password_backfill(self.acc_id, "manual")
        db.mark_account_password_backfill_running(self.acc_id)
        ok = db.update_account_password_backfill(
            self.acc_id,
            {"ok": True, "status": "success", "password": "Zx9!VeryLongPass1",
             "access_token": "tok2", "token_expires_at": "2026-12-01T00:00:00Z"})
        self.assertTrue(ok)
        row = db.get_account(self.acc_id)
        self.assertEqual(row["password_backfill_status"], "success")
        self.assertTrue(row["password_backfill_ok"])
        self.assertEqual(row["registration_password"], "Zx9!VeryLongPass1")
        self.assertEqual(row["access_token"], "tok2")
        self.assertEqual(row["token_expires_at"], "2026-12-01T00:00:00Z")
        # extra 其它键（注册素材 user）必须保留。
        extra = json.loads(row["extra_json"])
        self.assertIn("user", extra)
        self.assertEqual(extra["registration_password"], "Zx9!VeryLongPass1")
        self.assertEqual(db._extract_registration_password(row), "Zx9!VeryLongPass1")

    def test_failure_keeps_password_empty_and_writes_error(self):
        db.claim_account_password_backfill(self.acc_id, "manual")
        ok = db.update_account_password_backfill(
            self.acc_id, {"ok": False, "status": "failed", "error": "boom"})
        self.assertTrue(ok)
        row = db.get_account(self.acc_id)
        self.assertEqual(row["password_backfill_status"], "failed")
        self.assertEqual(row["password_backfill_error"], "boom")
        self.assertFalse(db._extract_registration_password(row))

    def test_recover_interrupted(self):
        db.claim_account_password_backfill(self.acc_id, "manual")
        db.mark_account_password_backfill_running(self.acc_id)
        interrupted = db.recover_interrupted_password_backfills()
        self.assertEqual(len(interrupted), 1)
        row = db.get_account(self.acc_id)
        self.assertEqual(row["password_backfill_status"], "failed")
        self.assertIn("中断", row["password_backfill_error"])

    def test_status_projection_contains_backfill(self):
        """轻量状态快照必须带补密码状态，前端轮询才能感知完成。"""
        db.claim_account_password_backfill(self.acc_id, "manual")
        snapshot = db.list_account_plan_check_statuses()
        item = next(x for x in snapshot["items"] if x["id"] == self.acc_id)
        self.assertEqual(item.get("password_backfill_status"), "queued")


class EnqueueGuardTest(unittest.TestCase):
    """enqueue 前置校验：来源限制与占用冲突。"""

    def test_non_outlook_source_rejected(self):
        from core import password_service as svc
        r = svc.enqueue_account_password_backfill(
            account_id=1, email="a@x.test", email_source="generic_api")
        self.assertFalse(r["accepted"])
        self.assertIn("outlook", r["error"])

    def test_empty_email_rejected(self):
        from core import password_service as svc
        r = svc.enqueue_account_password_backfill(account_id=1, email="", email_source="outlook")
        self.assertFalse(r["accepted"])
        self.assertEqual(r["error"], "email 为空")

    def test_busy_when_already_claimed(self):
        from core import password_service as svc
        with patch.object(svc, "apply_settings", return_value={}), \
             patch.object(svc._email_cfg, "USE_EMAIL_SERVICE", True), \
             patch("core.db.claim_account_password_backfill", return_value=False) as claim, \
             patch("core.db.update_account_password_backfill"):
            r = svc.enqueue_account_password_backfill(
                account_id=7, email="a@b.test", email_source="outlook")
        self.assertFalse(r["accepted"])
        self.assertTrue(r["busy"])
        claim.assert_called_once()


class PasswordPolicyTest(unittest.TestCase):
    def test_generator_respects_min_length(self):
        from core.openai_auth import generate_registration_password
        with patch("config.register.REGISTER_PASSWORD", ""):
            for _ in range(5):
                self.assertGreaterEqual(len(generate_registration_password(14)), 12)


if __name__ == "__main__":
    unittest.main()
