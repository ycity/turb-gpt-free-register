# -*- coding: utf-8 -*-
"""用实采的 Stripe 结账页 HTML 验证填表逻辑。

把采集到的 HTML 用本地 http server 提供，再用 Playwright 真实填一遍，
验证选择器、填写顺序和金额读取都对得上真实 DOM。

不连外网、不碰 VCC、不提交支付。缺少 HTML 样本或 Playwright 时自动跳过。
"""
import http.server
import socketserver
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "stripe_checkout_classic.html"
# Chromium 拒绝 5060/5061（ERR_UNSAFE_PORT），用 5070+
PORT = 5077


def _playwright_chromium():
    """返回可用的 chromium 启动参数；不可用则返回 None。"""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return None
    import os
    local = os.environ.get("LOCALAPPDATA", "")
    shell = Path(local) / ("ms-playwright/chromium_headless_shell-1243/"
                           "chrome-headless-shell-win64/chrome-headless-shell.exe")
    return {"executable_path": str(shell)} if shell.exists() else {}


@unittest.skipUnless(SAMPLE.exists(), f"缺少 HTML 样本：{SAMPLE}")
@unittest.skipIf(_playwright_chromium() is None, "未安装 playwright")
class CheckoutFillTest(unittest.TestCase):
    httpd = None
    thread = None

    @classmethod
    def setUpClass(cls):
        root = str(SAMPLE.parent)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=root, **kw)

            def log_message(self, *a):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        cls.httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        if cls.httpd:
            cls.httpd.shutdown()
            cls.httpd.server_close()

    def test_fill_against_real_dom(self):
        from playwright.sync_api import sync_playwright

        from core.vcc_checkout_pay import (
            PayResult, detect_variant, fill_checkout, read_total,
        )

        class Cfg:
            VCC_PAGE_TIMEOUT = 30.0
            VCC_ACCEPT_TERMS = True
            VCC_SAVE_PAYMENT_METHOD = True

        card = {"number": "5378140000001234", "exp_month": "12",
                "exp_year": "28", "cvc": "123"}
        billing = {"name": "Test Holder", "country": "US", "state": "AK",
                   "city": "Anchorage", "line1": "11901 Northern Raven Dr",
                   "line2": "", "postal": "99516"}
        result = PayResult()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **_playwright_chromium())
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{PORT}/{SAMPLE.name}",
                          wait_until="domcontentloaded")

                self.assertEqual(detect_variant(page), "classic")

                fill_checkout(page, card=card, email="probe@example.com",
                              billing=billing, cfg=Cfg(), result=result,
                              out_dir=Path(__file__).parent / "_fill_shots")

                # 每个必填项都真的写进了 DOM
                self.assertEqual(page.input_value("#email"), "probe@example.com")
                self.assertEqual(page.input_value("#cardNumber"), card["number"])
                self.assertEqual(page.input_value("#cardCvc"), "123")
                self.assertEqual(page.input_value("#billingName"), "Test Holder")
                self.assertEqual(page.input_value("#billingAddressLine1"),
                                 "11901 Northern Raven Dr")
                self.assertEqual(page.input_value("#billingLocality"), "Anchorage")
                self.assertEqual(page.input_value("#billingPostalCode"), "99516")
                self.assertEqual(page.input_value("select#billingCountry"), "US")

                # 州必须真的选中——它直接决定税费，不允许静默跳过
                self.assertEqual(page.input_value("select#billingAdministrativeArea"), "AK")
                self.assertTrue(result.filled.get("billing_state"))

                # 条款必须勾上，否则 Stripe 不允许提交
                self.assertTrue(page.is_checked("#termsOfServiceConsentCheckbox"))
                self.assertTrue(page.is_checked("#optInSetupFutureUsageCheckbox"))

                # 提交按钮存在且可点（但本测试不点）
                self.assertEqual(page.locator(
                    'button[data-testid="hosted-payment-submit-button"]').count(), 1)

                # 金额能从真实 DOM 读出来
                currency, amount = read_total(page)
                self.assertEqual(currency, "EGP")
                self.assertAlmostEqual(amount, 1311.00, places=2)
            finally:
                browser.close()

        self.assertTrue(result.filled.get("card_number"))
        self.assertTrue(result.filled.get("accept_terms"))

    def test_unselectable_state_raises_instead_of_filling_wrong_address(self):
        """州选不中时必须报错停下，不能当作选填项跳过。

        州决定税费；静默跳过会导致提交一个错误的账单归属地。
        """
        from playwright.sync_api import sync_playwright

        from core.vcc_checkout_pay import PayResult, VccPayError, fill_checkout

        class Cfg:
            VCC_PAGE_TIMEOUT = 30.0
            VCC_ACCEPT_TERMS = True
            VCC_SAVE_PAYMENT_METHOD = False

        billing = {"name": "Test Holder", "country": "US", "state": "ZZ",
                   "city": "Anchorage", "line1": "11901 Northern Raven Dr",
                   "line2": "", "postal": "99516"}

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **_playwright_chromium())
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{PORT}/{SAMPLE.name}",
                          wait_until="domcontentloaded")
                with self.assertRaises(VccPayError) as ctx:
                    fill_checkout(page,
                                  card={"number": "5378140000001234", "exp_month": "12",
                                        "exp_year": "28", "cvc": "123"},
                                  email="probe@example.com", billing=billing,
                                  cfg=Cfg(), result=PayResult(),
                                  out_dir=Path(__file__).parent / "_fill_shots")
                self.assertIn("billing_state", str(ctx.exception))
                # 关键：州没选中时页面上不能残留别的州
                self.assertEqual(page.input_value("select#billingAdministrativeArea"), "")
            finally:
                browser.close()

    def test_static_html_state_select_lists_egypt_governorates(self):
        """样本保留埃及省份：印证采集时国家已是 EG，并非未填。

        实采页在 EG 下只有埃及省份；本样本额外补了美国州码以便测成功路径。
        """
        html = SAMPLE.read_text(encoding="utf-8", errors="replace")
        self.assertIn("القاهرة", html)       # 开罗
        self.assertIn("EGP&nbsp;1,311.00", html)   # 含 14% VAT 的总额

    def test_guacamole_variant_is_refused_not_blindly_filled(self):
        from core.vcc_checkout_pay import PayResult, VccPayError, fill_checkout

        class Page:
            def get_attribute(self, *_a):
                return "guacamole"

        with self.assertRaises(VccPayError) as ctx:
            fill_checkout(Page(), card={}, email="", billing={},
                          cfg=object(), result=PayResult(), out_dir=Path("."))
        self.assertIn("guacamole", str(ctx.exception))


class _FakeVcc:
    def pick_card(self, **_kw):
        return {"cardId": "TEST", "cardNoMask": "537814******1234"}

    def pick_cardholder(self, _hid):
        return {"holderName": "Test Holder"}

    def card_secrets(self, _cid):
        return {"number": "5378140000001234", "exp_month": "11",
                "exp_year": "2029", "cvc": "123"}


def _handover_cfg(**over):
    from config import vcc as base

    class Cfg:
        pass

    for name in dir(base):
        if name.startswith("VCC_"):
            setattr(Cfg, name, getattr(base, name))
    Cfg.VCC_AUTO_PAY_ENABLED = True
    Cfg.VCC_KEEP_OPEN = True
    Cfg.VCC_KEEP_OPEN_TIMEOUT = 6.0
    Cfg.VCC_EXPECTED_AMOUNT = 1311.00
    Cfg.VCC_EXPECTED_CURRENCY = "EGP"
    Cfg.VCC_MAX_CHARGE = 1400.0
    Cfg.VCC_3DS_ENABLED = False
    Cfg.VCC_SCREENSHOT_DIR = str(Path(__file__).parent / "_fill_shots")
    for key, value in over.items():
        setattr(Cfg, key, value)
    return Cfg()


@unittest.skipUnless(SAMPLE.exists(), f"缺少 HTML 样本：{SAMPLE}")
@unittest.skipIf(_playwright_chromium() is None, "未安装 playwright")
class HandOverTest(unittest.TestCase):
    """交还窗口给人工点提交的行为。

    这些用例跑在无头内核上：交接逻辑本身与有头无关，只是人看不到窗口。
    """

    httpd = None
    thread = None

    @classmethod
    def setUpClass(cls):
        root = str(SAMPLE.parent)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=root, **kw)

            def log_message(self, *a):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        cls.httpd = socketserver.TCPServer(("127.0.0.1", PORT + 1), Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        if cls.httpd:
            cls.httpd.shutdown()
            cls.httpd.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{PORT + 1}/{SAMPLE.name}"

    def _run(self, cfg, *, on_handover=None):
        import core.vcc_checkout_pay as vp

        launch = _playwright_chromium()
        cfg.VCC_BROWSER_PATH = launch.get("executable_path", "")
        cfg.VCC_BROWSER_CHANNEL = ""
        cfg.VCC_HEADLESS = True

        original = vp._hand_over
        if on_handover is not None:
            def patched(page, c, result, out_dir, **kw):
                on_handover(page)
                return original(page, c, result, out_dir, **kw)
            vp._hand_over = patched
        try:
            return vp.pay_checkout(self.url, email="probe@example.com",
                                   cfg=cfg, client=_FakeVcc())
        finally:
            vp._hand_over = original

    def test_keep_open_overrides_auto_submit(self):
        """资金安全线：交还窗口时绝不能由程序替人点提交。"""
        result = self._run(_handover_cfg(VCC_AUTO_SUBMIT=True))
        self.assertFalse(result["submitted"])
        self.assertEqual(result["stage"], "human_timeout")

    def test_detects_human_submit(self):
        def leave_form(page):
            # 真实页面提交后会离开表单；这里在页面内定时移除提交按钮来模拟。
            # 不能跨线程操作 page：同步 Playwright 绑定在当前 greenlet 上。
            page.evaluate(
                """() => setTimeout(() => {
                    const b = document.querySelector(
                        'button[data-testid="hosted-payment-submit-button"]');
                    if (b) b.remove();
                }, 1500)""")

        result = self._run(_handover_cfg(), on_handover=leave_form)
        self.assertTrue(result["submitted"])
        self.assertEqual(result["stage"], "human_submitted")

    def test_gate_failure_still_hands_over_without_submitting(self):
        """闸门不过时也把窗口交给人，但必须如实报告差异且不代点。"""
        result = self._run(_handover_cfg(VCC_EXPECTED_AMOUNT=1150.00))
        self.assertFalse(result["submitted"])
        self.assertEqual(result["stage"], "human_timeout")
        self.assertAlmostEqual(result["amount"], 1311.00, places=2)

    def test_headed_without_channel_is_refused(self):
        """自带内核是无头版，要开窗口却没配通道时必须明确报错。"""
        import core.vcc_checkout_pay as vp

        cfg = _handover_cfg()
        cfg.VCC_BROWSER_CHANNEL = ""
        cfg.VCC_BROWSER_PATH = ""
        cfg.VCC_HEADLESS = True        # KEEP_OPEN 会强制有头
        with self.assertRaises(vp.VccPayError) as ctx:
            vp.pay_checkout(self.url, email="p@e.com", cfg=cfg, client=_FakeVcc())
        self.assertIn("headless shell", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
