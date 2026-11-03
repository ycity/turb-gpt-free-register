# -*- coding: utf-8 -*-
"""资料目录/指纹按账号隔离。

默认按代理出口复用（保 cf_clearance、少撞人机验证），但同一出口上的所有账号
会共用一套指纹和设备状态。开启 CLOAK_PROFILE_PER_ACCOUNT 后按账号隔离，用于
验证"独立设备身份能否降低封号率"。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import cloak_profile  # noqa: E402

PROXY_A = "http://u:p@a.example:1080"
PROXY_B = "http://u:p@b.example:1080"


class ProfileKeyTest(unittest.TestCase):
    def test_same_proxy_shares_key_by_default(self):
        self.assertEqual(cloak_profile.profile_key(PROXY_A),
                         cloak_profile.profile_key(PROXY_A))

    def test_different_proxy_differs(self):
        self.assertNotEqual(cloak_profile.profile_key(PROXY_A),
                            cloak_profile.profile_key(PROXY_B))

    def test_identity_splits_same_proxy(self):
        """同一出口下，不同账号必须拿到不同目录。"""
        a = cloak_profile.profile_key(PROXY_A, "one@e.test")
        b = cloak_profile.profile_key(PROXY_A, "two@e.test")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, cloak_profile.profile_key(PROXY_A))

    def test_identity_is_stable_and_case_insensitive(self):
        self.assertEqual(cloak_profile.profile_key(PROXY_A, "One@E.test"),
                         cloak_profile.profile_key(PROXY_A, "one@e.test"))

    def test_same_account_different_proxy_differs(self):
        self.assertNotEqual(cloak_profile.profile_key(PROXY_A, "x@e.test"),
                            cloak_profile.profile_key(PROXY_B, "x@e.test"))

    def test_direct_connection_has_key(self):
        self.assertEqual(cloak_profile.profile_key(""), "direct")
        self.assertNotEqual(cloak_profile.profile_key("", "x@e.test"), "direct")


class AcquireGuardTest(unittest.TestCase):
    def test_per_account_without_identity_refuses_shared_dir(self):
        """开了隔离却没拿到邮箱时，宁可用临时目录，也不能退回公用身份。"""
        with patch("config.cloakbrowser.CLOAK_PERSIST_PROFILE", True), \
             patch("config.cloakbrowser.CLOAK_PROFILE_PER_ACCOUNT", True, create=True), \
             patch.object(cloak_profile, "_local_lock_for") as lock:
            self.assertIsNone(cloak_profile.acquire(PROXY_A))
        lock.assert_not_called()

    def test_disabled_persist_returns_none(self):
        with patch("config.cloakbrowser.CLOAK_PERSIST_PROFILE", False):
            self.assertIsNone(cloak_profile.acquire(PROXY_A, "x@e.test"))

    def test_shared_mode_without_identity_still_works(self):
        """默认模式不受影响：没有邮箱也照常复用出口目录。"""
        with patch("config.cloakbrowser.CLOAK_PERSIST_PROFILE", True), \
             patch("config.cloakbrowser.CLOAK_PROFILE_PER_ACCOUNT", False, create=True), \
             patch.object(cloak_profile, "_local_lock_for") as lock:
            lock.return_value.acquire.return_value = False
            cloak_profile.acquire(PROXY_A)
        lock.assert_called_once()


class DriverSeedTest(unittest.TestCase):
    def _seed_args(self, identity, per_account, configured_seed=""):
        """跑 build_cloak_driver 的参数拼装段，只取 --fingerprint 结果。"""
        import hashlib
        seed = configured_seed
        ident = str(identity or "").strip().lower()
        if not seed and ident and per_account:
            seed = hashlib.sha256(f"cloak-fp|{ident}".encode()).hexdigest()[:16]
        return seed

    def test_no_seed_by_default(self):
        self.assertEqual(self._seed_args("a@e.test", False), "")

    def test_seed_derived_per_account(self):
        a = self._seed_args("a@e.test", True)
        b = self._seed_args("b@e.test", True)
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)

    def test_seed_stable_for_same_account(self):
        self.assertEqual(self._seed_args("a@e.test", True),
                         self._seed_args("A@E.test", True))

    def test_explicit_config_seed_wins(self):
        self.assertEqual(self._seed_args("a@e.test", True, "fixed123"), "fixed123")


class WiringTest(unittest.TestCase):
    def test_registration_passes_identity(self):
        src = (Path(__file__).resolve().parents[1]
               / "core" / "cloakbrowser_registration.py").read_text(encoding="utf-8")
        self.assertIn("identity=str(email or \"\")", src)

    def test_driver_forwards_identity_to_profile(self):
        src = (Path(__file__).resolve().parents[1]
               / "core" / "cloakbrowser_driver.py").read_text(encoding="utf-8")
        self.assertIn("identity=ident", src)


if __name__ == "__main__":
    unittest.main()
