# -*- coding: utf-8 -*-
"""重启不该静默丢掉正在跑的 2FA。

线上 #42：注册后 180 秒冷却刚过、2FA 刚启动，我为部署重启了容器，2FA 跑在
后台线程直接被打断，只留下一条 failed 等人手动点。而重启前的"空闲检查"只
看注册任务（active: 0），看不到 2FA。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402


def _app():
    import os
    os.environ.setdefault("WEBUI_AUTH_CODE", "t-code")
    from webui.app import create_app
    return create_app(auth_code="t-code")


HEAD = {"X-Auth-Code": "t-code"}


class RecoveryReturnsRowsTest(unittest.TestCase):
    def test_returns_rows_for_requeue(self):
        """只返回个数没法重排，必须带上邮箱和 token。"""
        rows = [{
            "id": 42, "email": "a@e.test", "totp_setup_status": "running",
            "access_token": "tok", "proxy_used": "http://p:1",
            "totp_setup_trigger": "registration_auto",
        }]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts"):
            out = db.recover_interrupted_totp_setups()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["email"], "a@e.test")
        self.assertEqual(out[0]["access_token"], "tok")
        self.assertEqual(out[0]["trigger"], "registration_auto")
        self.assertEqual(rows[0]["totp_setup_status"], "failed")
        self.assertIn("自动重新排队", rows[0]["totp_setup_error"])

    def test_only_touches_in_flight(self):
        rows = [
            {"id": 1, "email": "done@e.test", "totp_setup_status": "success"},
            {"id": 2, "email": "run@e.test", "totp_setup_status": "queued",
             "access_token": "t"},
        ]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts"):
            out = db.recover_interrupted_totp_setups()
        self.assertEqual([r["email"] for r in out], ["run@e.test"])
        self.assertEqual(rows[0]["totp_setup_status"], "success")

    def test_nothing_in_flight_writes_nothing(self):
        rows = [{"id": 1, "email": "a@e.test", "totp_setup_status": "success"}]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts") as save:
            self.assertEqual(db.recover_interrupted_totp_setups(), [])
        save.assert_not_called()


class RequeueTest(unittest.TestCase):
    def test_requeues_each_row(self):
        from webui import app as webapp
        rows = [{"id": 42, "email": "a@e.test", "access_token": "tok",
                 "proxy_used": "http://p:1", "trigger": "registration_auto"}]
        with patch("core.twofa_service.enqueue_account_totp_setup",
                   return_value={"accepted": True}) as eq:
            webapp._requeue_interrupted_totp(rows)
        self.assertEqual(eq.call_args.kwargs["email"], "a@e.test")
        self.assertEqual(eq.call_args.kwargs["account_id"], 42)

    def test_skips_rows_without_token(self):
        from webui import app as webapp
        with patch("core.twofa_service.enqueue_account_totp_setup") as eq:
            webapp._requeue_interrupted_totp(
                [{"id": 1, "email": "a@e.test", "access_token": ""}])
        eq.assert_not_called()

    def test_enqueue_failure_does_not_raise(self):
        """重排失败不能让整个启动流程崩掉。"""
        from webui import app as webapp
        with patch("core.twofa_service.enqueue_account_totp_setup",
                   side_effect=RuntimeError("queue full")):
            webapp._requeue_interrupted_totp(
                [{"id": 1, "email": "a@e.test", "access_token": "t"}])


class BusyApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _app().test_client()

    def test_reports_idle(self):
        with patch.object(db, "job_status_counts", return_value={"active": 0}), \
             patch.object(db, "totp_setup_activity", return_value={"pending": 0}), \
             patch.object(db, "list_accounts", return_value=[]), \
             patch("core.vcc_pay_session.list_active", return_value=None):
            r = self.client.get("/api/busy", headers=HEAD)
        b = r.get_json()
        self.assertTrue(b["ok"])
        self.assertFalse(b["busy"])

    def test_in_flight_totp_marks_busy(self):
        """这正是 #42 被漏掉的情形：注册空闲，但 2FA 在跑。"""
        with patch.object(db, "job_status_counts", return_value={"active": 0}), \
             patch.object(db, "totp_setup_activity",
                          return_value={"pending": 1,
                                        "pending_since": "2026-11-03T11:40:29"}), \
             patch.object(db, "list_accounts", return_value=[]), \
             patch("core.vcc_pay_session.list_active", return_value=None):
            r = self.client.get("/api/busy", headers=HEAD)
        b = r.get_json()
        self.assertTrue(b["busy"], "2FA 在跑时必须报 busy")
        self.assertEqual(b["detail"]["totp_setups"], 1)
        self.assertEqual(b["detail"]["totp_pending_since"], "2026-11-03T11:40:29")

    def test_live_check_marks_busy(self):
        with patch.object(db, "job_status_counts", return_value={"active": 0}), \
             patch.object(db, "totp_setup_activity", return_value={"pending": 0}), \
             patch.object(db, "list_accounts",
                          return_value=[{"live_check_status": "running"}]), \
             patch("core.vcc_pay_session.list_active", return_value=None):
            r = self.client.get("/api/busy", headers=HEAD)
        self.assertTrue(r.get_json()["busy"])

    def test_one_broken_probe_does_not_500(self):
        with patch.object(db, "job_status_counts",
                          side_effect=RuntimeError("db gone")), \
             patch.object(db, "totp_setup_activity", return_value={"pending": 0}), \
             patch.object(db, "list_accounts", return_value=[]), \
             patch("core.vcc_pay_session.list_active", return_value=None):
            r = self.client.get("/api/busy", headers=HEAD)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["detail"]["registration_jobs"], "查询失败")


if __name__ == "__main__":
    unittest.main()
