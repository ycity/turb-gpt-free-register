# -*- coding: utf-8 -*-
"""注册流程不能滑进登录路径。

线上 job #92：邮箱已注册过，页面上的「Continue with password」指向
/log-in/password，但选择器只按文字匹配就点了它，于是从"注册"滑进"登录"。
随后拿到的是登录验证码，填进注册流程被拒，代码又去点"重新发送"，最终
页面被踢回 /log-in 并报"找不到重发按钮"——真正的原因（邮箱已注册）被
完全掩盖，还白白耗掉 10 分钟和两轮重试。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import roxy_registration as rr  # noqa: E402


def _playwright_chromium():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return None
    import os
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return {}
    shell = Path(local) / ("ms-playwright/chromium_headless_shell-1243/"
                           "chrome-headless-shell-win64/chrome-headless-shell.exe")
    return {"executable_path": str(shell)} if shell.exists() else {}


def _driver(url: str):
    d = MagicMock()
    d.current_url = url
    return d


class LoginRouteDetectionTest(unittest.TestCase):
    def test_login_routes_detected(self):
        for url in ("https://auth.openai.com/log-in",
                    "https://auth.openai.com/log-in/password",
                    "https://auth.openai.com/LOG-IN/password"):
            self.assertTrue(rr._is_login_route(_driver(url)), url)

    def test_signup_routes_not_flagged(self):
        for url in ("https://auth.openai.com/create-account/password",
                    "https://auth.openai.com/email-verification",
                    "https://chatgpt.com/auth/login"):
            self.assertFalse(rr._is_login_route(_driver(url)), url)

    def test_unreadable_url_is_safe(self):
        d = MagicMock()
        type(d).current_url = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("detached")))
        self.assertFalse(rr._is_login_route(d))


class OtpGuardWiringTest(unittest.TestCase):
    def test_guard_runs_before_typing_otp(self):
        """守卫必须在填码之前，否则还是会把登录码填进注册流程。"""
        src = Path(rr.__file__).read_text(encoding="utf-8")
        guard = src.index("if _is_login_route(driver):")
        typing = src.index("_clear_otp_inputs(driver)\n            _type_otp")
        self.assertLess(guard, typing)

    def test_guard_raises_the_disable_marker(self):
        """错误文案要带停用标记，让服务层把邮箱剔除而不是放回池子。"""
        src = Path(rr.__file__).read_text(encoding="utf-8")
        seg = src[src.index("if _is_login_route(driver):"):]
        seg = seg[:seg.index("_clear_otp_inputs")]
        self.assertIn("按已注册/不可用邮箱处理并停用", seg)

    def test_service_disables_email_on_that_marker(self):
        from core import registration_service as svc
        msg = ("邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: "
               "url=https://auth.openai.com/log-in/password")
        self.assertTrue(svc._should_disable_failed_registration_email(msg))


class PasswordButtonSelectorTest(unittest.TestCase):
    """「Continue with password」在登录页和注册页文案一样，必须按 href 区分。"""

    PAGE = """
    <a id="signup" href="/create-account/password">Continue with password</a>
    <a id="login"  href="/log-in/password">Continue with password</a>
    """

    @classmethod
    def setUpClass(cls):
        launch = _playwright_chromium()
        if launch is None:
            raise unittest.SkipTest("未安装 playwright 或缺少 chromium")
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch(**launch)
        src = Path(rr.__file__).read_text(encoding="utf-8")
        seg = src[src.index("def _click_continue_with_password_if_present"):]
        import re
        m = re.search(r'execute_script\(r"""(.*?)"""\)', seg, re.S)
        assert m, "没抓到选择器脚本"
        cls.script = m.group(1)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "_pw", None):
            cls._pw.stop()

    def test_picks_signup_not_login(self):
        page = self.browser.new_page()
        try:
            page.set_content(self.PAGE)
            res = page.evaluate(f"() => {{ {self.script} }}")
            self.assertTrue(res.get("ok"), res)
            self.assertIn("/create-account/password", res.get("href", ""),
                          "选中了登录按钮，会把注册流程带进登录")
        finally:
            page.close()

    def test_login_only_page_yields_nothing(self):
        """只有登录按钮时宁可不点，也不能滑进登录流程。"""
        page = self.browser.new_page()
        try:
            page.set_content(
                '<a href="/log-in/password">Continue with password</a>')
            res = page.evaluate(f"() => {{ {self.script} }}")
            self.assertFalse(res.get("ok"), res)
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
