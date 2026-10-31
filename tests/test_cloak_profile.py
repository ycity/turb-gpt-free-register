# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import cloakbrowser as cloak_cfg
from core import cloak_profile


class _IsolatedProfileMixin:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for patcher in (
            patch.object(cloak_cfg, "CLOAK_PERSIST_PROFILE", True),
            patch.object(cloak_cfg, "CLOAK_PROFILE_ROOT", str(self.root)),
            patch.object(cloak_cfg, "CLOAK_PROFILE_MAX_IDLE_DAYS", 14),
            patch.dict(cloak_profile._LOCAL_LOCKS, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class ProfileKeyTests(unittest.TestCase):
    def test_key_is_stable_per_proxy_and_differs_between_proxies(self):
        a = cloak_profile.profile_key("socks5h://u:p@1.2.3.4:1080")
        self.assertEqual(a, cloak_profile.profile_key("socks5h://u:p@1.2.3.4:1080"))
        self.assertNotEqual(a, cloak_profile.profile_key("socks5h://u:p@5.6.7.8:1080"))
        self.assertEqual(cloak_profile.profile_key(""), "direct")
        self.assertNotIn("/", a)

    def test_relative_root_resolves_under_project(self):
        with patch.object(cloak_cfg, "CLOAK_PROFILE_ROOT", "cache/custom-profiles"):
            self.assertTrue(cloak_profile.profile_root().is_absolute())
            self.assertEqual(cloak_profile.profile_root().name, "custom-profiles")


class ProfileLeaseTests(_IsolatedProfileMixin, unittest.TestCase):
    def test_disabled_by_default_returns_no_lease(self):
        with patch.object(cloak_cfg, "CLOAK_PERSIST_PROFILE", False):
            self.assertIsNone(cloak_profile.acquire("socks5h://1.2.3.4:1080"))

    def test_same_proxy_reuses_directory_across_runs(self):
        first = cloak_profile.acquire("socks5h://1.2.3.4:1080")
        self.assertIsNotNone(first)
        (first.path / "marker").write_text("x", encoding="utf-8")
        first.release()

        second = cloak_profile.acquire("socks5h://1.2.3.4:1080")
        self.addCleanup(second.release)
        self.assertEqual(second.path, first.path)
        self.assertTrue((second.path / "marker").exists())

    def test_second_lease_on_busy_directory_falls_back(self):
        held = cloak_profile.acquire("socks5h://1.2.3.4:1080")
        self.addCleanup(held.release)
        self.assertIsNone(cloak_profile.acquire("socks5h://1.2.3.4:1080"))
        # 不同代理互不影响。
        other = cloak_profile.acquire("socks5h://5.6.7.8:1080")
        self.addCleanup(other.release)
        self.assertNotEqual(other.path, held.path)

    def test_release_frees_lock_and_is_idempotent(self):
        lease = cloak_profile.acquire("socks5h://1.2.3.4:1080")
        lease.release()
        lease.release()
        self.assertFalse((lease.path / cloak_profile._LOCK_NAME).exists())
        again = cloak_profile.acquire("socks5h://1.2.3.4:1080")
        self.addCleanup(again.release)
        self.assertIsNotNone(again)

    def test_concurrent_acquire_grants_exactly_one(self):
        results, lock = [], threading.Lock()

        def worker():
            lease = cloak_profile.acquire("socks5h://1.2.3.4:1080")
            with lock:
                results.append(lease)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        granted = [r for r in results if r is not None]
        self.addCleanup(lambda: [r.release() for r in granted])
        self.assertEqual(len(granted), 1)

    def test_stale_lock_from_dead_process_is_taken_over(self):
        path = self.root / cloak_profile.profile_key("socks5h://1.2.3.4:1080")
        path.mkdir(parents=True)
        lock_file = path / cloak_profile._LOCK_NAME
        lock_file.write_text("99999", encoding="utf-8")
        old = time.time() - cloak_profile._STALE_LOCK_SECONDS - 60
        os.utime(lock_file, (old, old))

        lease = cloak_profile.acquire("socks5h://1.2.3.4:1080")
        self.assertIsNotNone(lease)
        self.addCleanup(lease.release)

    def test_fresh_lock_from_live_process_is_respected(self):
        path = self.root / cloak_profile.profile_key("socks5h://1.2.3.4:1080")
        path.mkdir(parents=True)
        (path / cloak_profile._LOCK_NAME).write_text("1234", encoding="utf-8")
        self.assertIsNone(cloak_profile.acquire("socks5h://1.2.3.4:1080"))


class CookieCleanupTests(unittest.TestCase):
    def _context(self, cookies):
        ctx = MagicMock()
        ctx.cookies.return_value = cookies
        return ctx

    def test_keeps_cloudflare_cookies_and_drops_login_state(self):
        cookies = [
            {"name": "cf_clearance", "domain": ".chatgpt.com", "value": "cf"},
            {"name": "__cf_bm", "domain": ".chatgpt.com", "value": "bm"},
            {"name": "__Secure-next-auth.session-token", "domain": "chatgpt.com", "value": "secret"},
            {"name": "oai-did", "domain": ".openai.com", "value": "dev"},
        ]
        ctx = self._context(cookies)

        result = cloak_profile.keep_only_cloudflare_cookies(ctx)

        self.assertEqual((result["kept"], result["dropped"]), (2, 2))
        ctx.clear_cookies.assert_called_once()
        kept = ctx.add_cookies.call_args[0][0]
        self.assertEqual({c["name"] for c in kept}, {"cf_clearance", "__cf_bm"})

    def test_no_cloudflare_cookie_still_clears_everything(self):
        ctx = self._context([{"name": "__Secure-next-auth.session-token", "domain": "chatgpt.com"}])

        result = cloak_profile.keep_only_cloudflare_cookies(ctx)

        self.assertEqual((result["kept"], result["dropped"]), (0, 1))
        ctx.clear_cookies.assert_called_once()
        ctx.add_cookies.assert_not_called()

    def test_clear_failure_is_reported_so_caller_can_drop_the_profile(self):
        ctx = self._context([{"name": "__Secure-next-auth.session-token", "domain": "chatgpt.com"}])
        ctx.clear_cookies.side_effect = RuntimeError("context closed")

        self.assertEqual(cloak_profile.keep_only_cloudflare_cookies(ctx)["kept"], -1)

    def test_read_failure_is_reported(self):
        ctx = MagicMock()
        ctx.cookies.side_effect = RuntimeError("boom")

        self.assertEqual(cloak_profile.keep_only_cloudflare_cookies(ctx)["kept"], -1)
        ctx.clear_cookies.assert_not_called()


class IdleCleanupTests(_IsolatedProfileMixin, unittest.TestCase):
    def _aged_dir(self, name: str, days: float, *, locked: bool = False) -> Path:
        path = self.root / name
        path.mkdir(parents=True)
        if locked:
            (path / cloak_profile._LOCK_NAME).write_text("1", encoding="utf-8")
        # 写锁文件会刷新目录 mtime，必须最后再改时间，否则这个目录根本走不到锁判断。
        when = time.time() - days * 86400
        os.utime(path, (when, when))
        return path

    def test_removes_only_idle_unlocked_directories(self):
        stale = self._aged_dir("stale", 30)
        fresh = self._aged_dir("fresh", 1)
        busy = self._aged_dir("busy", 30, locked=True)

        self.assertEqual(cloak_profile.cleanup_idle_profiles(), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(busy.exists())

    def test_zero_days_disables_cleanup(self):
        stale = self._aged_dir("stale", 90)
        with patch.object(cloak_cfg, "CLOAK_PROFILE_MAX_IDLE_DAYS", 0):
            self.assertEqual(cloak_profile.cleanup_idle_profiles(), 0)
        self.assertTrue(stale.exists())

    def test_missing_root_is_not_an_error(self):
        with patch.object(cloak_cfg, "CLOAK_PROFILE_ROOT", str(self.root / "nope")):
            self.assertEqual(cloak_profile.cleanup_idle_profiles(), 0)


class DriverIntegrationTests(_IsolatedProfileMixin, unittest.TestCase):
    """build_cloak_driver 里的资料目录接线：复用、清理、失败回退、退出释放。"""

    def _run_build(self, *, cookies=None, clear_raises=False):
        from core import cloakbrowser_driver

        context = MagicMock()
        context.cookies.return_value = cookies if cookies is not None else []
        if clear_raises:
            context.clear_cookies.side_effect = RuntimeError("context closed")
        plain_browser, plain_context = MagicMock(), MagicMock()
        plain_browser.new_context.return_value = plain_context
        fake_module = MagicMock()
        fake_module.launch_persistent_context.return_value = context
        fake_module.launch.return_value = plain_browser

        with patch.dict("sys.modules", {"cloakbrowser": fake_module}), \
                patch.object(cloak_cfg, "CLOAK_USER_DATA_DIR", ""), \
                patch.object(cloak_cfg, "CLOAK_USE_PROXY", False), \
                patch.object(cloakbrowser_driver, "_build_cloak_locale_options", return_value={}):
            driver, _ = cloakbrowser_driver.build_cloak_driver(proxy="")
        return driver, fake_module, context, plain_browser

    def test_persistent_context_is_used_and_lock_is_released_on_quit(self):
        driver, module, context, plain_browser = self._run_build(
            cookies=[{"name": "cf_clearance", "domain": ".chatgpt.com"},
                     {"name": "__Secure-next-auth.session-token", "domain": "chatgpt.com"}]
        )
        used_dir = Path(module.launch_persistent_context.call_args[0][0])
        self.assertEqual(used_dir.parent, self.root)
        self.assertTrue((used_dir / cloak_profile._LOCK_NAME).exists())
        # 登录态被清掉，只留 Cloudflare cookie。
        context.clear_cookies.assert_called_once()
        self.assertEqual([c["name"] for c in context.add_cookies.call_args[0][0]], ["cf_clearance"])
        plain_browser.new_context.assert_not_called()

        driver.quit()
        self.assertFalse((used_dir / cloak_profile._LOCK_NAME).exists())
        # 释放后同一代理可以再次取得。
        again = cloak_profile.acquire("")
        self.assertIsNotNone(again)
        again.release()

    def test_cookie_clear_failure_falls_back_to_temp_context_and_frees_lock(self):
        driver, module, context, plain_browser = self._run_build(
            cookies=[{"name": "__Secure-next-auth.session-token", "domain": "chatgpt.com"}],
            clear_raises=True,
        )
        used_dir = Path(module.launch_persistent_context.call_args[0][0])
        # 放弃复用：持久化上下文已关闭，改用全新浏览器，锁立即释放。
        context.close.assert_called_once()
        plain_browser.new_context.assert_called_once()
        self.assertIs(driver.browser, plain_browser)
        self.assertFalse((used_dir / cloak_profile._LOCK_NAME).exists())

    def test_launch_failure_releases_lock(self):
        from core import cloakbrowser_driver

        fake_module = MagicMock()
        fake_module.launch_persistent_context.side_effect = RuntimeError("launch failed")
        with patch.dict("sys.modules", {"cloakbrowser": fake_module}), \
                patch.object(cloak_cfg, "CLOAK_USER_DATA_DIR", ""), \
                patch.object(cloak_cfg, "CLOAK_USE_PROXY", False), \
                patch.object(cloakbrowser_driver, "_build_cloak_locale_options", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                cloakbrowser_driver.build_cloak_driver(proxy="")

        lease = cloak_profile.acquire("")
        self.assertIsNotNone(lease)
        lease.release()

    def test_explicit_user_data_dir_keeps_old_behaviour(self):
        from core import cloakbrowser_driver

        fixed = self.root / "fixed-debug-profile"
        context = MagicMock()
        fake_module = MagicMock()
        fake_module.launch_persistent_context.return_value = context
        with patch.dict("sys.modules", {"cloakbrowser": fake_module}), \
                patch.object(cloak_cfg, "CLOAK_USER_DATA_DIR", str(fixed)), \
                patch.object(cloak_cfg, "CLOAK_USE_PROXY", False), \
                patch.object(cloakbrowser_driver, "_build_cloak_locale_options", return_value={}):
            cloakbrowser_driver.build_cloak_driver(proxy="")

        self.assertEqual(fake_module.launch_persistent_context.call_args[0][0], str(fixed))
        # 固定目录是调试用途，保持原行为：不按代理分目录，也不动 cookie。
        context.clear_cookies.assert_not_called()
        self.assertFalse((self.root / cloak_profile.profile_key("")).exists())


if __name__ == "__main__":
    unittest.main()
