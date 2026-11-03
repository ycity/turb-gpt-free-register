# -*- coding: utf-8 -*-
"""2FA 重认证后必须把新 accessToken 写回账号。

线上现象：9 个账号里 7 个的 token 返回 401 token_revoked，看着像被风控封号，
实际是我们自己作废的——setup_2fa 内部 reauth 换到新 token 用于 enroll/activate，
但只 return secret，新 token 被丢弃，存档里始终是注册时那个已失效的 token。
"""
import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import account_export, db, twofa_service  # noqa: E402


class TokenOutContractTest(unittest.TestCase):
    def test_setup_2fa_accepts_token_out(self):
        import inspect
        sig = inspect.signature(account_export.setup_2fa)
        self.assertIn("token_out", sig.parameters)

    def test_return_value_still_secret(self):
        """返回值不能改成元组，既有调用方和测试都按字符串用。"""
        import inspect
        src = inspect.getsource(account_export.setup_2fa)
        self.assertIn("return secret", src)

    def test_token_written_before_enroll(self):
        """回传要在 enroll 之前：enroll 失败时旧 token 也已经废了。"""
        src = Path(account_export.__file__).read_text(encoding="utf-8")
        seg = src[src.index("new_token = _exchange_new_token"):]
        write = seg.index('token_out["access_token"] = new_token')
        enroll = seg.index("_enroll_totp(session, new_token)")
        self.assertLess(write, enroll)


class DbUpdateTest(unittest.TestCase):
    def test_updates_token(self):
        rows = [{"id": 7, "email": "a@e.test", "access_token": "old"}]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts") as save:
            self.assertTrue(db.update_account_access_token(7, "new"))
        self.assertEqual(rows[0]["access_token"], "new")
        self.assertIn("access_token_updated_at", rows[0])
        save.assert_called_once()

    def test_same_token_is_noop(self):
        rows = [{"id": 7, "email": "a@e.test", "access_token": "same"}]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts") as save:
            self.assertFalse(db.update_account_access_token(7, "same"))
        save.assert_not_called()

    def test_empty_token_rejected(self):
        with patch.object(db, "_load_accounts") as load:
            self.assertFalse(db.update_account_access_token(7, ""))
            self.assertFalse(db.update_account_access_token(7, "   "))
        load.assert_not_called()

    def test_unknown_account_is_safe(self):
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_save_accounts") as save:
            self.assertFalse(db.update_account_access_token(99, "tok"))
        save.assert_not_called()


class PersistHelperTest(unittest.TestCase):
    def test_persists_token(self):
        with patch.object(db, "update_account_access_token", return_value=True) as up, \
             patch.object(twofa_service, "_append_log"):
            twofa_service._persist_refreshed_token(7, "a@e.test", {"access_token": "new"})
        up.assert_called_once_with(7, "new")

    def test_empty_container_is_noop(self):
        with patch.object(db, "update_account_access_token") as up:
            twofa_service._persist_refreshed_token(7, "a@e.test", {})
            twofa_service._persist_refreshed_token(7, "a@e.test", None)
        up.assert_not_called()

    def test_db_failure_does_not_raise(self):
        """回写失败不能把一次成功的 2FA 变成异常。"""
        with patch.object(db, "update_account_access_token",
                          side_effect=RuntimeError("db locked")), \
             patch.object(twofa_service, "_append_log"):
            twofa_service._persist_refreshed_token(7, "a@e.test", {"access_token": "t"})


class ServiceWiringTest(unittest.TestCase):
    def _src(self) -> str:
        return Path(twofa_service.__file__).read_text(encoding="utf-8")

    def test_uses_finally_so_failure_also_persists(self):
        """2FA 失败时旧 token 同样已被作废，必须照样回写。"""
        src = self._src()
        seg = src[src.index("refreshed: dict = {}"):]
        seg = seg[:seg.index("db.update_account_totp_secret")]
        self.assertIn("finally:", seg)
        self.assertIn("_persist_refreshed_token", seg)

    def test_passes_token_out(self):
        self.assertIn("token_out=refreshed", self._src())


class CliWiringTest(unittest.TestCase):
    def test_main_overwrites_access_token(self):
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        seg = src[src.index("totp_secret = setup_2fa"):]
        seg = seg[:seg.index("阶段 7.5")]
        self.assertIn("token_out=refreshed", seg)
        self.assertIn("access_token = new_token", seg)
        self.assertIn("finally:", seg)


if __name__ == "__main__":
    unittest.main()
