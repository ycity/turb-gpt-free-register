# -*- coding: utf-8 -*-
"""长链探活与出口回退。

对应线上事故：账号自己的代理（softbank 出口）打不开 Stripe 结账页，
表单永远等不出来，报成一条看不懂的 Playwright 超时；而同一条长链换
个出口立刻可用——长链没失效，优惠码本不该被判死。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vcc_checkout_pay as vp  # noqa: E402


def _probe(result_by_proxy):
    """按代理返回预设探测结果。"""
    def fake(url, *, proxy="", timeout=30.0):
        return dict(result_by_proxy[proxy])
    return fake


class ProbeFallbackTest(unittest.TestCase):
    URL = "https://pay.openai.com/c/pay/cs_live_x"

    def test_usable_on_own_proxy_returns_immediately(self):
        calls = []

        def fake(url, *, proxy="", timeout=30.0):
            calls.append(proxy)
            return {"usable": True, "kind": "ready", "reason": "表单已就绪"}

        with patch.object(vp, "probe_checkout_link", side_effect=fake):
            res = vp.probe_with_fallback(self.URL, proxy="P1", candidates=["P2", "P3"])
        self.assertTrue(res["usable"])
        self.assertEqual(res["proxy"], "P1")
        self.assertEqual(calls, ["P1"], "本单代理就能用时不该再试别的出口")

    def test_blank_falls_back_to_working_proxy(self):
        """这是线上那一单：本代理空白，备用出口可用。"""
        table = {
            "softbank": {"usable": False, "kind": "blank", "reason": "页面空白，该出口加载不出 Stripe 资源"},
            "good": {"usable": True, "kind": "ready", "reason": "表单已就绪"},
        }
        with patch.object(vp, "probe_checkout_link", side_effect=_probe(table)):
            res = vp.probe_with_fallback(self.URL, proxy="softbank", candidates=["good"])
        self.assertTrue(res["usable"])
        self.assertEqual(res["proxy"], "good")
        self.assertTrue(res["switched"])
        self.assertIn("空白", res["original_reason"])

    def test_expired_does_not_waste_time_on_other_proxies(self):
        """会话过期换谁都一样，不该继续探。"""
        calls = []

        def fake(url, *, proxy="", timeout=30.0):
            calls.append(proxy)
            return {"usable": False, "kind": "expired", "reason": "结账会话已过期或已完成"}

        with patch.object(vp, "probe_checkout_link", side_effect=fake):
            res = vp.probe_with_fallback(self.URL, proxy="P1", candidates=["P2", "P3"])
        self.assertFalse(res["usable"])
        self.assertEqual(res["kind"], "expired")
        self.assertEqual(calls, ["P1"])

    def test_all_proxies_blank_reports_original_reason(self):
        with patch.object(vp, "probe_checkout_link",
                          side_effect=lambda u, *, proxy="", timeout=30.0: {
                              "usable": False, "kind": "blank", "reason": "页面空白"}):
            res = vp.probe_with_fallback(self.URL, proxy="P1", candidates=["P2"])
        self.assertFalse(res["usable"])
        self.assertEqual(res["proxy"], "P1")
        self.assertNotIn("switched", res)

    def test_same_proxy_in_candidates_is_skipped(self):
        calls = []

        def fake(url, *, proxy="", timeout=30.0):
            calls.append(proxy)
            return {"usable": False, "kind": "blank", "reason": "空白"}

        with patch.object(vp, "probe_checkout_link", side_effect=fake):
            vp.probe_with_fallback(self.URL, proxy="P1", candidates=["P1", "", "P2"])
        self.assertEqual(calls, ["P1", "P2"], "重复和空代理不该浪费一次探测")

    def test_empty_url_is_rejected(self):
        self.assertFalse(vp.probe_checkout_link("")["usable"])


class ExpiredMarkerTest(unittest.TestCase):
    def test_real_stripe_wording_is_recognised(self):
        # 线上实际抓到的文案，含花体撇号
        for text in (
            "You’re all done here You’ve either completed your payment or this checkout session has timed out.",
            "You're all done here",
            "THIS CHECKOUT SESSION HAS TIMED OUT",
        ):
            self.assertTrue(
                any(m in text.lower() for m in vp._EXPIRED_MARKERS),
                f"没认出过期文案：{text[:40]}")

    def test_normal_page_is_not_flagged_expired(self):
        text = "Subscribe to ChatGPT Team Pay with card Total due today"
        self.assertFalse(any(m in text.lower() for m in vp._EXPIRED_MARKERS))


if __name__ == "__main__":
    unittest.main()
