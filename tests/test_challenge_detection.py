# -*- coding: utf-8 -*-
import time
import unittest
from unittest.mock import MagicMock, patch

from core import roxy_registration as rr


class _FakeDriver:
    """用一段真实 DOM 片段驱动 detect_challenge 的 JS，逐步换页模拟挑战通过。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.current_url = "https://chatgpt.com/auth/login"
        self.refreshed = 0
        self.script_calls = 0

    def _page(self):
        return self.pages[0] if len(self.pages) == 1 else self.pages.pop(0)

    def execute_script(self, script, *args):
        self.script_calls += 1
        page = self._page()
        if page is _RAISE:
            raise RuntimeError("page closed")
        return dict(page)

    def refresh(self):
        self.refreshed += 1


_RAISE = object()
# detect_challenge 的 JS 返回值形状。
_CLEAR = {"active": False, "kind": "", "title": "ChatGPT", "url": "https://chatgpt.com/auth/login"}
_INTERSTITIAL = {"active": True, "kind": "interstitial", "title": "Just a moment...", "url": "https://chatgpt.com/auth/login"}
_WIDGET = {"active": True, "kind": "widget", "title": "ChatGPT", "url": "https://chatgpt.com/auth/login"}


class DetectChallengeScriptTests(unittest.TestCase):
    """在真实浏览器里渲染 Cloudflare 页面特征，确认注入的 JS 选择器写对了。

    用 CloakBrowser 自带的 Chromium（和注册时同一个）；没有浏览器就跳过。
    """

    @classmethod
    def setUpClass(cls):
        try:
            from cloakbrowser import launch
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("未安装 cloakbrowser")
        try:
            cls._browser = launch(headless=True, humanize=False, geoip=False)
        except Exception as exc:  # pragma: no cover - 没有浏览器二进制时跳过
            raise unittest.SkipTest(f"无法启动 CloakBrowser：{exc}")
        cls._page = cls._browser.new_context().new_page()
        cls._page.set_default_timeout(8000)
        cls._page.goto("about:blank")

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()

    def _detect(self, html: str, *, title: str = "ChatGPT") -> dict:
        # set_content 在 CloakBrowser 的 stealth 页面上会卡住，直接写 DOM。
        self._page.evaluate("([t, h]) => { document.title = t; document.body.innerHTML = h; }", [title, html])
        driver = MagicMock()
        driver.current_url = self._page.url
        driver.execute_script.side_effect = lambda script, *a: self._page.evaluate("() => {" + script + "}")
        return rr.detect_challenge(driver)

    def test_normal_login_page_is_not_a_challenge(self):
        html = '<form><input type="email" name="username"><button type="submit">Continue</button></form>'
        self.assertFalse(self._detect(html)["active"])

    def test_interstitial_page_is_detected(self):
        self.assertEqual(self._detect("<div id='cf-challenge-running'></div>")["kind"], "interstitial")
        self.assertEqual(self._detect("<div class='cf-browser-verification'></div>")["kind"], "interstitial")
        self.assertEqual(self._detect("<p>loading</p>", title="Just a moment...")["kind"], "interstitial")

    def test_turnstile_widget_in_form_is_detected(self):
        html = ('<form><input type="email" name="username">'
                '<div class="cf-turnstile"></div></form>')
        self.assertEqual(self._detect(html)["kind"], "widget")
        self.assertEqual(self._detect('<div id="cf-chl-widget-abc"></div>')["kind"], "widget")
        self.assertEqual(
            self._detect('<iframe src="https://challenges.cloudflare.com/x" width="300" height="65"></iframe>')["kind"],
            "widget",
        )

    def test_hidden_turnstile_iframe_alone_is_not_treated_as_blocking(self):
        html = ('<iframe src="https://challenges.cloudflare.com/x" style="display:none"></iframe>'
                '<form><input type="email" name="username"></form>')
        self.assertFalse(self._detect(html)["active"])


class DetectChallengeTests(unittest.TestCase):
    def test_script_failure_is_treated_as_no_challenge(self):
        result = rr.detect_challenge(_FakeDriver([_RAISE]))
        self.assertFalse(result["active"])
        self.assertIn("RuntimeError", result["detail"])

    def test_non_dict_result_is_tolerated(self):
        driver = MagicMock()
        driver.execute_script.return_value = "unexpected"
        self.assertFalse(rr.detect_challenge(driver)["active"])


class WaitOutChallengeTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.object(rr, "_CHALLENGE_POLL_SECONDS", 0.01),
            patch.object(rr, "_check_manual_stop", lambda: None),
            patch.object(rr, "_log_prefix", lambda d: "[测试]"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_returns_false_immediately_when_no_challenge(self):
        driver = _FakeDriver([_CLEAR])
        self.assertFalse(rr.wait_out_challenge(driver))
        self.assertEqual(driver.script_calls, 1)

    def test_waits_until_challenge_clears_without_refreshing(self):
        driver = _FakeDriver([_INTERSTITIAL, _INTERSTITIAL, _INTERSTITIAL, _CLEAR])
        self.assertTrue(rr.wait_out_challenge(driver))
        self.assertEqual(driver.refreshed, 0)

    def test_done_callback_stops_the_wait_early(self):
        driver = _FakeDriver([_WIDGET])
        calls = []

        def done():
            calls.append(1)
            return len(calls) >= 2

        self.assertTrue(rr.wait_out_challenge(driver, done=done))
        self.assertEqual(len(calls), 2)

    def test_done_callback_errors_do_not_break_the_wait(self):
        driver = _FakeDriver([_WIDGET, _WIDGET, _CLEAR])
        self.assertTrue(rr.wait_out_challenge(driver, done=MagicMock(side_effect=RuntimeError("boom"))))

    def test_timeout_still_reports_a_challenge_was_seen(self):
        driver = _FakeDriver([_INTERSTITIAL])
        started = time.time()
        self.assertTrue(rr.wait_out_challenge(driver, timeout=0.05))
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(driver.refreshed, 0)

    def test_manual_stop_propagates(self):
        driver = _FakeDriver([_INTERSTITIAL])
        with patch.object(rr, "_check_manual_stop", side_effect=RuntimeError("用户已停止")):
            with self.assertRaisesRegex(RuntimeError, "用户已停止"):
                rr.wait_out_challenge(driver)


class RefreshGuardTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.object(rr, "_CHALLENGE_POLL_SECONDS", 0.01),
            patch.object(rr, "_check_manual_stop", lambda: None),
            patch.object(rr, "_log_prefix", lambda d: "[测试]"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_challenge_suppresses_the_refresh(self):
        driver = _FakeDriver([_INTERSTITIAL, _CLEAR])
        self.assertTrue(rr._refresh_after_missing_page_element(driver, "邮箱输入框", 0))
        self.assertEqual(driver.refreshed, 0, "挑战进行中绝不能刷新，否则挑战会重来")

    def test_plain_missing_element_still_refreshes(self):
        driver = _FakeDriver([_CLEAR])
        self.assertTrue(rr._refresh_after_missing_page_element(driver, "邮箱输入框", 0))
        self.assertEqual(driver.refreshed, 1)

    def test_retry_budget_is_still_respected(self):
        driver = _FakeDriver([_CLEAR])
        self.assertFalse(
            rr._refresh_after_missing_page_element(driver, "邮箱输入框", rr._MISSING_PAGE_ELEMENT_REFRESH_RETRIES)
        )
        self.assertEqual(driver.refreshed, 0)
        self.assertEqual(driver.script_calls, 0, "超出重试次数时不该再探测页面")

    def test_refresh_failure_is_reported(self):
        driver = _FakeDriver([_CLEAR])
        driver.refresh = MagicMock(side_effect=RuntimeError("browser gone"))
        self.assertFalse(rr._refresh_after_missing_page_element(driver, "邮箱输入框", 0))


class SubmitEmailChallengeFlowTests(unittest.TestCase):
    """提交邮箱后卡住时，必须先等挑战，不能立刻重交或打 NextAuth 接口。"""

    def setUp(self):
        for patcher in (
            patch.object(rr, "_CHALLENGE_POLL_SECONDS", 0.01),
            patch.object(rr, "_check_manual_stop", lambda: None),
            patch.object(rr, "_log_prefix", lambda d: "[测试]"),
            patch.object(rr, "human_delay", lambda *a, **k: None),
            patch.object(rr, "_type_email_address", lambda *a, **k: None),
            patch.object(rr, "_submit_email_step", MagicMock()),
            patch.object(rr, "_email_input_value_state", lambda d: {"url": "u", "inputs": [{"value": "a@b.com"}]}),
            patch.object(rr, "_is_email_login_page_still_present", lambda d: True),
            patch.object(rr, "_wait_email_submit_next_state", lambda d, e, timeout=20: "email_page"),
            patch("time.sleep", lambda s: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_challenge_then_otp_returns_without_calling_nextauth(self):
        states = [None, None, "otp"]
        nextauth = MagicMock()
        with patch.object(rr, "detect_challenge", side_effect=[
            {"active": True, "kind": "widget", "detail": ""},
            {"active": False, "kind": "", "detail": ""},
        ]), \
             patch.object(rr, "_current_email_submit_next_state", side_effect=lambda d: states.pop(0) if states else "otp"), \
             patch.object(rr, "_submit_email_via_browser_nextauth", nextauth):
            self.assertEqual(rr._submit_email_and_wait_next(MagicMock(), "a@b.com", attempts=3), "otp")
        nextauth.assert_not_called()

    def test_final_error_names_the_challenge(self):
        with patch.object(rr, "detect_challenge", return_value={"active": True, "kind": "interstitial", "detail": ""}), \
             patch.object(rr, "_current_email_submit_next_state", return_value=None), \
             patch.object(rr, "_submit_email_via_browser_nextauth", return_value={"ok": False}), \
             patch.object(rr, "wait_out_challenge", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Cloudflare 人机验证"):
                rr._submit_email_and_wait_next(MagicMock(), "a@b.com", attempts=1)

    def test_error_without_challenge_keeps_the_plain_message(self):
        with patch.object(rr, "detect_challenge", return_value={"active": False, "kind": "", "detail": ""}), \
             patch.object(rr, "_current_email_submit_next_state", return_value=None), \
             patch.object(rr, "_submit_email_via_browser_nextauth", return_value={"ok": False}):
            with self.assertRaises(RuntimeError) as ctx:
                rr._submit_email_and_wait_next(MagicMock(), "a@b.com", attempts=1)
        self.assertNotIn("人机验证", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
