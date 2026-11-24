# -*- coding: utf-8 -*-
"""更换 2FA 密钥功能（B 方案）的接口与服务测试。

背景（2026-11-19）：#149/#155 实测——已有 TOTP 的账号，只要用旧密钥
完成一次完整登录，recent_auth 窗口内直接 POST mfa/enroll 就能拿新
secret，激活后旧密钥作废。不需要先禁用旧验证器，也不需要邮箱 OTP。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class ServiceTest(unittest.TestCase):
    def test_module_imports(self):
        from core import totp_rotate_service as svc
        self.assertTrue(callable(svc.rotate_totp_secret))
        self.assertTrue(callable(svc.is_running))

    def test_no_secret_rejected(self):
        """没开 2FA 的账号没有可换的密钥，应明确拒绝而不是报错。"""
        from core import totp_rotate_service as svc
        with patch("core.db.get_account", return_value={"id": 1, "email": "a@b.test", "totp_secret": ""}):
            r = svc.rotate_totp_secret(1)
        self.assertFalse(r["ok"])
        self.assertIn("没有已启用", r["error"])

    def test_missing_account_rejected(self):
        from core import totp_rotate_service as svc
        with patch("core.db.get_account", return_value=None):
            r = svc.rotate_totp_secret(99)
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])

    def test_backup_written_before_rotate(self):
        """换密钥前必须先备份旧密钥——激活失败时这是唯一回滚线索。"""
        from core import totp_rotate_service as svc
        with patch.object(svc, "_backup_old_secret",
                          return_value=Path("/tmp/x.json")) as bk, \
             patch.object(svc, "_fresh_login", return_value=("tok", "http://p:1")), \
             patch.object(svc, "_enroll_and_activate", return_value="NEWSECRET"), \
             patch("core.db.get_account", return_value={
                 "id": 1, "email": "a@b.test", "totp_secret": "OLD"}), \
             patch("core.db._load_accounts", return_value=[]), \
             patch("core.db._save_accounts"):
            r = svc.rotate_totp_secret(1)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["new_secret"], "NEWSECRET")
        self.assertGreaterEqual(bk.call_count, 1, "没有先备份旧密钥")

    def test_backup_precedes_enroll_order(self):
        """备份 -> 登录 -> enroll 的顺序不能乱：enroll 一旦发出，
        activate 失败时旧密钥状态未知，没有备份就回不了头。"""
        from core import totp_rotate_service as svc
        order = []
        with patch.object(svc, "_backup_old_secret",
                          side_effect=lambda *a: order.append("backup") or Path("/tmp/x.json")), \
             patch.object(svc, "_fresh_login",
                          side_effect=lambda *a: order.append("login") or ("tok", "http://p:1")), \
             patch.object(svc, "_enroll_and_activate",
                          side_effect=lambda *a: order.append("enroll") or "NEW"), \
             patch("core.db.get_account", return_value={
                 "id": 1, "email": "a@b.test", "totp_secret": "OLD"}), \
             patch("core.db._load_accounts", return_value=[]), \
             patch("core.db._save_accounts"):
            svc.rotate_totp_secret(1)
        self.assertEqual(order[:3], ["backup", "login", "enroll"])

    def test_rotate_failure_returns_error_not_raises(self):
        """失败要以 {"ok": False} 返回，不能抛——前端和批量循环都靠它。"""
        from core import totp_rotate_service as svc
        with patch.object(svc, "_fresh_login",
                          side_effect=RuntimeError("所有出口都无法登录")), \
             patch.object(svc, "_backup_old_secret", return_value=Path("/tmp/x.json")), \
             patch("core.db.get_account", return_value={
                 "id": 1, "email": "a@b.test", "totp_secret": "OLD"}):
            r = svc.rotate_totp_secret(1)
        self.assertFalse(r["ok"])
        self.assertIn("所有出口", r["error"])

    def test_running_guard(self):
        """同一账号并发触发只允许一个。"""
        from core import totp_rotate_service as svc
        with patch("core.db.get_account", return_value={
                "id": 5, "email": "a@b.test", "totp_secret": "OLD"}):
            svc._running.add(5)
            try:
                r = svc.rotate_totp_secret(5)
            finally:
                svc._running.discard(5)
        self.assertFalse(r["ok"])
        self.assertIn("正在更换", r["error"])

    def test_fresh_login_stops_on_deactivated(self):
        """账号已废时换出口也没用，应立刻停。"""
        import core.account_liveness as liveness
        from core import totp_rotate_service as svc
        dead = {"ok": False, "status": "deactivated", "error": "account_deactivated"}
        # 给一个出口让循环真正跑起来；死号应立即停而不是试完全部出口
        pool = ["http://u:p@1.2.3.4:8080#测试出口", "http://u:p@2.3.4.5:8080#测试出口2"]
        with patch.object(liveness, "check_account_liveness", return_value=dead), \
             patch("core.db.get_account", return_value={
                 "id": 1, "email": "a@b.test", "email_source": "akmoz"}), \
             patch("webui.config_editor.get_field_value", return_value=pool):
            with self.assertRaises(RuntimeError) as ctx:
                svc._fresh_login(1)
            self.assertIn("停用", str(ctx.exception))
            # 死号不该继续烧第二个出口
            self.assertEqual(liveness.check_account_liveness.call_count, 1)


class ApiWiringTest(unittest.TestCase):
    """接口与前端按钮的存在性（源码断言，不需要 flask）。"""

    def test_backend_routes_exist(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        self.assertIn("/api/accounts/<int:acc_id>/totp-rotate", src)
        self.assertIn("/api/accounts/totp-rotate-bulk", src)
        self.assertIn("rotate_totp_secret", src)
        self.assertIn("totp_rotate_service", src)

    def test_backend_rejects_no_secret(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def api_account_totp_rotate")
        seg = src[i:i + 2500]
        self.assertIn("totp_secret", seg)
        self.assertIn("未开启 2FA", seg)

    def test_bulk_route_uses_serial_thread(self):
        """批量必须串行：并发登录会互相把出口打限流。"""
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def api_accounts_totp_rotate_bulk")
        seg = src[i:i + 6000]
        self.assertIn("for acc_id in targets", seg)
        self.assertIn("sleep(30)", seg)

    def test_frontend_menu_button_exists(self):
        src = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("rotateTotp(", src)
        self.assertIn("更换密钥", src)
        # 只有已开 2FA 的账号才显示按钮
        self.assertIn("r.totp_secret ?", src)

    def test_frontend_bulk_button_exists(self):
        src = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("rotateTotpBulk", src)
        self.assertIn("批量换密钥", src)
        self.assertIn("totp-rotate-bulk", src)

    def test_frontend_confirm_before_rotate(self):
        """换密钥不可逆（旧密钥作废），必须先弹确认。"""
        src = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        i = src.index("async function rotateTotp(")
        seg = src[i:i + 1500]
        self.assertIn("confirm(", seg)
        self.assertIn("旧密钥随即作废", seg)

    def test_bulk_confirm_exists(self):
        src = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        i = src.index("async function rotateTotpBulk(")
        seg = src[i:i + 1200]
        self.assertIn("confirm(", seg)


if __name__ == "__main__":
    unittest.main()
