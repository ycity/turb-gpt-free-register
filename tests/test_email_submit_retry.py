# -*- coding: utf-8 -*-
"""邮箱提交/密码提交两处失败路径的回归。

对应线上 job #53（邮箱提交三次重试全撞 Cloudflare 403）和 job #48
（密码页兜底提交被 requestSubmit 的 submitter 约束打成 TypeError）。
"""
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from core import roxy_registration as rr


def _playwright_chromium():
    """返回可用的 chromium 启动参数；不可用则返回 None。"""
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


def _resubmit_script() -> str:
    """抽出 _resubmit_signup_password_form 里的 JS，避免测试和实现各写一份。"""
    src = Path(rr.__file__).read_text(encoding="utf-8")
    m = re.search(
        r'def _resubmit_signup_password_form.*?execute_script\(r"""(.*?)"""\)',
        src, re.S,
    )
    if not m:
        raise AssertionError("没能从实现里抽出兜底提交脚本")
    return m.group(1)


class NextAuthBlockedTests(unittest.TestCase):
    def test_403_and_429_count_as_blocked(self):
        self.assertTrue(rr._nextauth_blocked({"ok": False, "status": 403}))
        self.assertTrue(rr._nextauth_blocked({"ok": False, "status": 429}))

    def test_business_failures_are_not_blocked(self):
        self.assertFalse(rr._nextauth_blocked({"ok": False, "status": 200}))
        self.assertFalse(rr._nextauth_blocked({"ok": True, "status": 200}))
        self.assertFalse(rr._nextauth_blocked({"ok": False}))

    def test_non_dict_is_not_blocked(self):
        # 兜底抛异常时 na_res 保持空字典，不能误判为被封。
        self.assertFalse(rr._nextauth_blocked(None))
        self.assertFalse(rr._nextauth_blocked("403"))
        self.assertFalse(rr._nextauth_blocked({}))

    def test_garbage_status_does_not_raise(self):
        self.assertFalse(rr._nextauth_blocked({"status": "oops"}))


class EmailRetryDelayTests(unittest.TestCase):
    def test_normal_failure_keeps_short_delay(self):
        # 普通失败不该被拖慢。
        self.assertEqual(rr._email_retry_delay(blocked_403=False, attempt=1), 1.0)
        self.assertEqual(rr._email_retry_delay(blocked_403=False, attempt=3), 1.0)

    def test_blocked_backs_off_exponentially(self):
        with patch.object(rr._cfg, "ROXY_EMAIL_BLOCKED_RETRY_DELAY", 20.0, create=True):
            self.assertEqual(rr._email_retry_delay(blocked_403=True, attempt=1), 20.0)
            self.assertEqual(rr._email_retry_delay(blocked_403=True, attempt=2), 40.0)
            self.assertEqual(rr._email_retry_delay(blocked_403=True, attempt=3), 80.0)

    def test_blocked_delay_is_capped(self):
        with patch.object(rr._cfg, "ROXY_EMAIL_BLOCKED_RETRY_DELAY", 20.0, create=True):
            self.assertEqual(rr._email_retry_delay(blocked_403=True, attempt=9),
                             rr._EMAIL_BLOCKED_RETRY_DELAY_CAP)

    def test_zero_config_disables_extra_wait(self):
        with patch.object(rr._cfg, "ROXY_EMAIL_BLOCKED_RETRY_DELAY", 0, create=True):
            self.assertEqual(rr._email_retry_delay(blocked_403=True, attempt=1), 0.0)

    def test_bad_config_falls_back_to_default(self):
        with patch.object(rr._cfg, "ROXY_EMAIL_BLOCKED_RETRY_DELAY", "abc", create=True):
            self.assertEqual(rr._email_retry_delay(blocked_403=True, attempt=1),
                             rr._EMAIL_BLOCKED_RETRY_DELAY_SECONDS)

    def test_blocked_delay_clears_a_minute_scale_window(self):
        """三次重试累计必须跨出分钟级封锁窗口，这才是本次修复的目的。"""
        with patch.object(rr._cfg, "ROXY_EMAIL_BLOCKED_RETRY_DELAY", 20.0, create=True):
            total = sum(rr._email_retry_delay(blocked_403=True, attempt=i) for i in (1, 2))
        self.assertGreaterEqual(total, 60.0)


class SleepWithStopTests(unittest.TestCase):
    def test_sleep_checks_manual_stop(self):
        """长等必须分段并在每段检查停止信号，否则手动停止要等满整段。"""
        checked = []
        clock = {"t": 0.0}
        with patch.object(rr, "_check_manual_stop", side_effect=lambda: checked.append(1)), \
             patch.object(rr.time, "sleep", side_effect=lambda d: clock.__setitem__("t", clock["t"] + max(d, 0.5))), \
             patch.object(rr.time, "time", side_effect=lambda: clock["t"]):
            rr._sleep_with_stop(6.0)
        self.assertGreaterEqual(len(checked), 2)

    def test_zero_seconds_does_not_sleep(self):
        slept = []
        with patch.object(rr, "_check_manual_stop"), \
             patch.object(rr.time, "sleep", side_effect=slept.append):
            rr._sleep_with_stop(0)
        self.assertEqual(slept, [])

    def test_stop_signal_aborts_the_wait(self):
        class Stop(RuntimeError):
            pass

        clock = {"t": 0.0}
        with patch.object(rr, "_check_manual_stop", side_effect=Stop("stopped")), \
             patch.object(rr.time, "sleep"), \
             patch.object(rr.time, "time", side_effect=lambda: clock["t"]):
            with self.assertRaises(Stop):
                rr._sleep_with_stop(90.0)


class ResubmitPasswordFormScriptTests(unittest.TestCase):
    """#48 的根因：requestSubmit(submitter) 只接受提交按钮。"""

    def _script(self) -> str:
        captured = {}

        class FakeDriver:
            def execute_script(self, script, *args):
                captured["script"] = script
                return {"ok": True}

        rr._resubmit_signup_password_form(FakeDriver())
        return captured["script"]

    def test_submitter_query_excludes_plain_buttons(self):
        script = self._script()
        # 旧实现用裸 `button` 选到 type=button，requestSubmit 直接 TypeError。
        self.assertIn('button:not([type])', script)
        self.assertNotIn('input[type="submit"],button\']', script)

    def test_script_has_fallback_when_request_submit_throws(self):
        script = self._script()
        self.assertIn("catch", script)
        self.assertIn("button_click_after_error", script)

    def test_reports_how_it_submitted(self):
        script = self._script()
        self.assertIn("requestSubmit_with_submitter", script)
        self.assertIn("'how'" if "'how'" in script else "how", script)

    def test_driver_exception_is_reported_not_raised(self):
        class BoomDriver:
            def execute_script(self, *a, **k):
                raise RuntimeError("ctx destroyed")

        res = rr._resubmit_signup_password_form(BoomDriver())
        self.assertFalse(res["ok"])
        self.assertIn("ctx destroyed", res["reason"])


class ResubmitPasswordFormDomTest(unittest.TestCase):
    """用真实 Chromium 验证兜底提交脚本，复刻 #48 的按钮结构。

    #48 的密码页 buttons 是 [type=button, type=submit, name=intent]；旧实现用
    裸 `button` 选择器选中第一个 type=button，requestSubmit 抛 TypeError，
    整次兜底提交作废。纯字符串断言抓不到这种规范层面的错，必须跑真实 DOM。
    """

    PAGE = """
    <form action="/create-account/password" onsubmit="window.__submitted=true; return false;">
      <input type="hidden" name="username" value="u@e.test">
      <input type="password" name="new-password" autocomplete="new-password">
      <button type="button">Back</button>
      <button type="submit">Continue</button>
      <button name="intent">intent</button>
    </form>
    """

    @classmethod
    def setUpClass(cls):
        launch = _playwright_chromium()
        if launch is None:
            raise unittest.SkipTest("未安装 playwright 或缺少 chromium")
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch(**launch)
        cls.script = _resubmit_script()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "_pw", None):
            cls._pw.stop()

    def _run(self, html, *, password="Secret12345678"):
        page = self.browser.new_page()
        try:
            page.set_content(html)
            if password:
                page.fill("input[type=password]", password)
            result = page.evaluate(f"() => {{ {self.script} }}")
            submitted = page.evaluate("() => window.__submitted === true")
            return result, submitted
        finally:
            page.close()

    def test_submits_with_proper_submitter(self):
        res, submitted = self._run(self.PAGE)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["how"], "requestSubmit_with_submitter", res)
        self.assertTrue(submitted, "表单没有真正提交")

    def test_submits_when_no_explicit_submit_button(self):
        html = self.PAGE.replace('<button type="submit">Continue</button>', "")
        res, submitted = self._run(html)
        self.assertTrue(res["ok"], res)
        self.assertTrue(submitted, "无 type=submit 时表单没有提交")

    def test_empty_password_is_refused(self):
        res, submitted = self._run(self.PAGE, password=None)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "empty_password")
        self.assertFalse(submitted)

    def test_page_errors_are_reported_instead_of_resubmitting(self):
        html = self.PAGE.replace("</form>", '<div role="alert">Password too weak</div></form>')
        res, submitted = self._run(html)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "page_errors")
        self.assertIn("Password too weak", " ".join(res["errors"]))
        self.assertFalse(submitted)

    def test_old_selector_really_did_throw(self):
        """反例固化：证明裸 button 选择器确实触发 #48 的 TypeError。"""
        page = self.browser.new_page()
        try:
            page.set_content(self.PAGE)
            page.fill("input[type=password]", "Secret12345678")
            old = page.evaluate("""() => {
              const form = document.querySelector('form');
              const button = [...form.querySelectorAll('button[type="submit"],input[type="submit"],button')][0];
              try { form.requestSubmit(button); return {threw:false, picked:button.type}; }
              catch (e) { return {threw:true, picked:button.type, err:String(e)}; }
            }""")
        finally:
            page.close()
        self.assertTrue(old["threw"], old)
        self.assertEqual(old["picked"], "button")
        self.assertIn("requestSubmit", old["err"])


if __name__ == "__main__":
    unittest.main()
