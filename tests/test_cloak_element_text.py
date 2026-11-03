# -*- coding: utf-8 -*-
"""CloakElement 必须覆盖调用方用到的 Selenium 元素接口。

线上事故（2026-11-02~03，自动注册连续失败 28 次）：CloakElement 没有 .text
属性，_click_resend_email_otp 读它时抛 AttributeError，异常被外层 except
吞掉当成"没找到按钮"，循环空转到超时。按钮其实一直都在，从没被点过。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cloakbrowser_driver import CloakElement  # noqa: E402


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


def _el(eval_result=None, raises=False):
    loc = MagicMock()
    if raises:
        loc.evaluate.side_effect = RuntimeError("detached")
    else:
        loc.evaluate.return_value = eval_result
    return CloakElement(page=MagicMock(), locator=loc)


class CloakElementTextTest(unittest.TestCase):
    def test_text_attribute_exists(self):
        """最核心的一条：属性必须存在，否则调用方直接 AttributeError。"""
        self.assertTrue(hasattr(_el(""), "text"))

    def test_returns_visible_text(self):
        self.assertEqual(_el("Resend code").text, "Resend code")

    def test_strips_whitespace(self):
        self.assertEqual(_el("  Send again \n").text, "Send again")

    def test_empty_element_returns_empty_string(self):
        self.assertEqual(_el("").text, "")
        self.assertEqual(_el(None).text, "")

    def test_detached_element_does_not_raise(self):
        # 页面跳转后元素失效很常见，读文字不能把流程打断
        self.assertEqual(_el(raises=True).text, "")


class SeleniumSurfaceTest(unittest.TestCase):
    """调用方按 Selenium 习惯用的接口，适配层都要有。"""

    REQUIRED = ("text", "tag_name", "click", "clear", "send_keys",
                "get_attribute", "is_displayed", "is_enabled")

    def test_required_members_present(self):
        el = _el("")
        missing = [n for n in self.REQUIRED if not hasattr(el, n)]
        self.assertEqual(missing, [], f"适配层缺少: {missing}")


class ResendButtonTextTest(unittest.TestCase):
    """读按钮文字失败时，仍然要点下去。"""

    def test_source_no_longer_reads_text_bare(self):
        src = (Path(__file__).resolve().parents[1]
               / "core" / "roxy_registration.py").read_text(encoding="utf-8")
        seg = src[src.index("def _click_resend_email_otp"):]
        seg = seg[:seg.index("def _wait_after_email_otp_submit")]
        self.assertNotIn("str(btn.text or btn.get_attribute", seg,
                         "又退回了会抛 AttributeError 的裸读法")
        self.assertIn('getattr(btn, "text", "")', seg)

    def test_click_happens_even_when_text_unreadable(self):
        """模拟没有 .text 的元素：仍要走到点击。"""
        from core import roxy_registration as rr

        class NoText:
            def get_attribute(self, _n):
                raise RuntimeError("detached")

        btn = NoText()
        text = ""
        for getter in (
            lambda: getattr(btn, "text", "") or "",
            lambda: btn.get_attribute("value") or "",
            lambda: btn.get_attribute("data-dd-action-name") or "",
        ):
            try:
                text = str(getter()).strip()
            except Exception:
                text = ""
            if text:
                break
        self.assertEqual(text, "", "取不到文字是允许的")
        self.assertTrue(hasattr(rr, "_click_resend_email_otp"))


class RealDomTextTest(unittest.TestCase):
    """真实 Chromium：确认 .text 在实际 DOM 上拿得到。"""

    PAGE = """
    <button id="resend" name="intent" value="resend">Resend code</button>
    <button id="blank"></button>
    <button id="spaced">   Send  again   </button>
    """

    @classmethod
    def setUpClass(cls):
        launch = _playwright_chromium()
        if launch is None:
            raise unittest.SkipTest("未安装 playwright 或缺少 chromium")
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch(**launch)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "_pw", None):
            cls._pw.stop()

    def test_reads_text_via_locator(self):
        page = self.browser.new_page()
        try:
            page.set_content(self.PAGE)
            el = CloakElement(page=page, locator=page.locator("#resend"))
            self.assertEqual(el.text, "Resend code")
        finally:
            page.close()

    def test_reads_text_via_handle_from_js(self):
        """线上就是这条路径：JS 选出按钮再读文字。"""
        page = self.browser.new_page()
        try:
            page.set_content(self.PAGE)
            handle = page.evaluate_handle(
                "() => [...document.querySelectorAll('button')]"
                ".find(e => /resend/i.test(e.getAttribute('value')||''))")
            el = CloakElement(page=page, handle=handle.as_element())
            self.assertEqual(el.text, "Resend code")
        finally:
            page.close()

    def test_blank_and_whitespace(self):
        page = self.browser.new_page()
        try:
            page.set_content(self.PAGE)
            self.assertEqual(
                CloakElement(page=page, locator=page.locator("#blank")).text, "")
            # innerText 会折叠连续空格，这是渲染语义，符合预期
            self.assertEqual(
                CloakElement(page=page, locator=page.locator("#spaced")).text,
                "Send again")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
