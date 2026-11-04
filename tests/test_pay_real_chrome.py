# -*- coding: utf-8 -*-
"""裸 Playwright 支付路径的反自动化配置。

背景：CloakBrowser 的内核由它自己的 ensure_binary() 决定，外部换不了。
想用真 Google Chrome 只能走 VCC_USE_CLOAK=False + VCC_BROWSER_CHANNEL=chrome
这条裸路径，所以这条路径必须自己补上 CloakBrowser 本来提供的那些伪装。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class FakePw:
    def __init__(self):
        self.kwargs = None

        class Chromium:
            def launch(inner, **kw):
                self.kwargs = kw
                return object()

        self.chromium = Chromium()


def _cfg(**over):
    from config import vcc as base

    class Cfg:
        pass

    for name in dir(base):
        if name.startswith("VCC_"):
            setattr(Cfg, name, getattr(base, name))
    for key, value in over.items():
        setattr(Cfg, key, value)
    return Cfg()


class PlainBrowserLaunchTest(unittest.TestCase):
    def _launch(self, cfg, proxy=""):
        from core.vcc_pay_session import _plain_browser
        pw = FakePw()
        _plain_browser(pw, cfg, proxy)
        return pw.kwargs

    def test_automation_flags_are_suppressed(self):
        kw = self._launch(_cfg())
        self.assertIn("--disable-blink-features=AutomationControlled",
                      kw.get("args") or [])
        self.assertIn("--enable-automation", kw.get("ignore_default_args") or [])

    def test_channel_selects_real_chrome(self):
        kw = self._launch(_cfg(VCC_BROWSER_CHANNEL="chrome"))
        self.assertEqual(kw.get("channel"), "chrome")
        self.assertNotIn("executable_path", kw)

    def test_channel_wins_over_explicit_path(self):
        kw = self._launch(_cfg(VCC_BROWSER_CHANNEL="chrome",
                               VCC_BROWSER_PATH="/opt/other/chrome"))
        self.assertEqual(kw.get("channel"), "chrome")
        self.assertNotIn("executable_path", kw)

    def test_explicit_path_used_when_no_channel(self):
        kw = self._launch(_cfg(VCC_BROWSER_CHANNEL="",
                               VCC_BROWSER_PATH="/opt/other/chrome"))
        self.assertEqual(kw.get("executable_path"), "/opt/other/chrome")
        self.assertNotIn("channel", kw)

    def test_headed_follows_server_headed(self):
        self.assertFalse(self._launch(_cfg(VCC_SERVER_HEADED=True))["headless"])
        self.assertTrue(self._launch(_cfg(VCC_SERVER_HEADED=False))["headless"])

    def test_proxy_is_passed_through(self):
        kw = self._launch(_cfg(), proxy="http://u:p@1.2.3.4:8080")
        self.assertIn("proxy", kw)

    def test_no_proxy_key_when_direct(self):
        self.assertNotIn("proxy", self._launch(_cfg(), proxy=""))


class CloakCannotSwapKernelTest(unittest.TestCase):
    """记录为什么不能直接让 CloakBrowser 用系统 Chrome。"""

    def test_cloak_launch_opts_do_not_carry_channel(self):
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        start = src.index("def _try_cloak_browser")
        body = src[start:src.index("def _plain_browser")]
        # CloakBrowser 的 executable_path 由它内部 ensure_binary() 决定
        self.assertNotIn('opts["channel"]', body)
        self.assertNotIn('opts["executable_path"]', body)


class PlainContextHardeningTest(unittest.TestCase):
    """裸路径的 context 必须补时区语言和 webdriver 抹除。"""

    def setUp(self):
        self.src = (ROOT / "core"
                    / "vcc_pay_session.py").read_text(encoding="utf-8")

    def test_locale_and_timezone_applied(self):
        self.assertIn("_build_cloak_locale_options", self.src)
        self.assertIn("timezone_id", self.src)
        self.assertIn("Accept-Language", self.src)

    def test_webdriver_is_masked(self):
        self.assertIn("navigator,'webdriver'", self.src)

    def test_hardening_only_on_plain_path(self):
        """CloakBrowser 自己做了这些，重复设置反而可能冲突。"""
        idx = self.src.index("navigator,'webdriver'")
        window = self.src[max(0, idx - 300):idx]
        self.assertIn("pw_ctx is not None", window)

    def test_locale_block_guarded_by_plain_path(self):
        idx = self.src.index("_build_cloak_locale_options")
        window = self.src[max(0, idx - 400):idx]
        self.assertIn("pw_ctx is not None", window)


class DockerfileChromeTest(unittest.TestCase):
    def setUp(self):
        self.df = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_installs_google_chrome(self):
        self.assertIn("google-chrome-stable", self.df)

    def test_uses_keyring_not_apt_key(self):
        """apt-key 在新版 Debian 已废弃，构建会失败。"""
        self.assertIn("/etc/apt/keyrings/google-chrome.asc", self.df)
        self.assertNotIn("apt-key add", self.df)

    def test_keyring_dir_created(self):
        self.assertIn("mkdir -p /etc/apt/keyrings", self.df)

    def test_signed_by_is_wired(self):
        self.assertIn("signed-by=/etc/apt/keyrings/google-chrome.asc", self.df)


if __name__ == "__main__":
    unittest.main(verbosity=2)
