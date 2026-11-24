# -*- coding: utf-8 -*-
"""打码接入：2Captcha 解 Stripe 的 hCaptcha Enterprise。

为什么需要（2026-11-18）：
  Stripe 在「加卡」和「提交支付」都会弹 hCaptcha Enterprise，带 rqdata
  （与当次 SetupIntent/PaymentIntent 绑定的签名）。实测 12 个出口（含美国
  住宅）全部触发，换卡、换余额都绕不开，自动流程只能停在 awaiting_human。
  #110 的补跑就卡在这一步。

关键约束：
  - rqdata 每次会话现生成，必须"先抓参数→提交→注入同一个浏览器"，
    不能预先批量解，也不能复用上一次的 sitekey/rqdata。
  - 打码失败必须回落人工，不能成为新的单点故障。
  - token 约 2 分钟有效，解出来要马上注入。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

from core import captcha_solver as cs  # noqa: E402


class _FakeResp:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SolveTest(unittest.TestCase):
    """solve_hcaptcha 的提交/轮询/错误处理。"""

    def test_submits_enterprise_with_rqdata_and_ua(self):
        sent = {}

        def fake_post(url, data, timeout):
            sent.update(data)
            return "OK|12345"

        def fake_get(url, timeout):
            return "OK|the-token"

        with patch.object(cs, "_post", fake_post), \
             patch.object(cs, "_get", fake_get), \
             patch.object(cs.time, "sleep"):
            tok = cs.solve_hcaptcha(
                api_key="KEY", sitekey="c7faac4c-x", pageurl="https://pay.openai.com/p",
                user_agent="UA/1", rqdata="RQ==", enterprise=True,
                timeout=30, poll_interval=0)

        self.assertEqual(tok, "the-token")
        self.assertEqual(sent["method"], "hcaptcha")
        self.assertEqual(sent["sitekey"], "c7faac4c-x")
        self.assertEqual(sent["pageurl"], "https://pay.openai.com/p")
        self.assertEqual(sent["enterprise"], "1")
        self.assertEqual(sent["rqdata"], "RQ==")
        self.assertEqual(sent["userAgent"], "UA/1")
        self.assertEqual(sent["json"], "1")

    def test_omits_enterprise_when_not_enterprise(self):
        sent = {}

        def fake_post(url, data, timeout):
            sent.update(data)
            return "OK|1"

        with patch.object(cs, "_post", fake_post), \
             patch.object(cs, "_get", lambda u, t: "OK|tok"), \
             patch.object(cs.time, "sleep"):
            cs.solve_hcaptcha(api_key="K", sitekey="s", pageurl="p",
                              enterprise=False, poll_interval=0)
        self.assertNotIn("enterprise", sent)

    def test_requires_key_and_sitekey(self):
        for kw in ({"api_key": "", "sitekey": "s", "pageurl": "p"},
                   {"api_key": "k", "sitekey": "", "pageurl": "p"},
                   {"api_key": "k", "sitekey": "s", "pageurl": ""}):
            with self.assertRaises(cs.CaptchaError):
                cs.solve_hcaptcha(**kw)

    def test_rejected_task_raises(self):
        with patch.object(cs, "_post", lambda u, d, t: "ERROR_WRONG_USER_KEY"):
            with self.assertRaises(cs.CaptchaError) as ctx:
                cs.solve_hcaptcha(api_key="bad", sitekey="s", pageurl="p")
            self.assertIn("被拒", str(ctx.exception))

    def test_no_slot_retries_then_gives_up(self):
        calls = {"n": 0}

        def fake_post(url, data, timeout):
            calls["n"] += 1
            return "ERROR_NO_SLOT_AVAILABLE"

        with patch.object(cs, "_post", fake_post), \
             patch.object(cs.time, "sleep"):
            with self.assertRaises(cs.CaptchaError):
                cs.solve_hcaptcha(api_key="k", sitekey="s", pageurl="p")
        self.assertEqual(calls["n"], 3)

    def test_keeps_polling_until_ready(self):
        seq = ["CAPCHA_NOT_READY", "CAPCHA_NOT_READY", "OK|final"]

        def fake_get(url, timeout):
            return seq.pop(0)

        with patch.object(cs, "_post", lambda u, d, t: "OK|9"), \
             patch.object(cs, "_get", fake_get), \
             patch.object(cs.time, "sleep"):
            self.assertEqual(
                cs.solve_hcaptcha(api_key="k", sitekey="s", pageurl="p",
                                  poll_interval=0, timeout=60),
                "final")

    def test_timeout_raises(self):
        with patch.object(cs, "_post", lambda u, d, t: "OK|9"), \
             patch.object(cs, "_get", lambda u, t: "CAPCHA_NOT_READY"), \
             patch.object(cs.time, "sleep"), \
             patch.object(cs.time, "time", side_effect=[0, 0, 0, 999]):
            with self.assertRaises(cs.CaptchaError) as ctx:
                cs.solve_hcaptcha(api_key="k", sitekey="s", pageurl="p",
                                  timeout=10, poll_interval=0)
            self.assertIn("超时", str(ctx.exception))


class WiringTest(unittest.TestCase):
    """打码必须接在支付流程里，且失败要回落人工。"""

    def test_vcc_pay_session_has_solver_hooks(self):
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        for fn in ("_extract_hcaptcha_params", "_inject_hcaptcha_token",
                   "_try_solve_captcha"):
            self.assertIn("def " + fn, src)
            self.assertIn(fn + "(", src)

    def test_captcha_loop_tries_solver_before_human(self):
        """先打码、后人工：打码只是加速，不能抢掉人工这条兜底。"""
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        i = src.index('while outcome == "captcha"')
        seg = src[i:i + 1800]
        self.assertIn("_try_solve_captcha", seg)
        self.assertIn("_human_takeover", seg)
        self.assertLess(seg.index("_try_solve_captcha"),
                        seg.index("_human_takeover"))

    def test_solver_failure_does_not_raise(self):
        """打码挂了只能返回 False，不能把整笔支付带崩。"""
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        i = src.index("def _try_solve_captcha")
        seg = src[i:i + 2600]
        self.assertIn("return False", seg)
        self.assertIn("CaptchaError", seg)

    def test_disabled_by_default(self):
        import config.vcc as vcc
        self.assertFalse(vcc.CAPTCHA_SOLVER_ENABLED)

    def test_config_keys_exist(self):
        src = (ROOT / "config" / "vcc.py").read_text(encoding="utf-8")
        for key in ("CAPTCHA_SOLVER_ENABLED", "CAPTCHA_2CAPTCHA_KEY",
                    "CAPTCHA_SOLVE_TIMEOUT", "CAPTCHA_POLL_INTERVAL"):
            self.assertIn("CAPTCHA", src)
            self.assertIn(f"'{key}'", src)


class PayCfgSnapshotTest(unittest.TestCase):
    """支付配置快照必须带上打码开关。

    2026-11-18 事故：_build_pay_cfg 只复制 VCC_ 前缀的字段，CAPTCHA_*
    全被丢掉，于是 getattr(cfg, "CAPTCHA_SOLVER_ENABLED", False) 恒为
    False——打码根本没被调用，#110 提交后 12 秒就落回人工验证码。
    """

    def _build(self, monkeypatch_attrs):
        import types
        from webui.app import _build_pay_cfg

        base = types.SimpleNamespace(**monkeypatch_attrs)
        return _build_pay_cfg(base, {})

    def test_captcha_fields_survive_snapshot(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("本机没装 flask，容器内会跑这条")
        cfg = self._build({
            "VCC_AUTO_PAY_ENABLED": True,
            "CAPTCHA_SOLVER_ENABLED": True,
            "CAPTCHA_2CAPTCHA_KEY": "KEY-1",
            "CAPTCHA_SOLVE_TIMEOUT": 180.0,
            "CAPTCHA_POLL_INTERVAL": 5.0,
            "OTHER_SETTING": "should-not-copy",
        })
        self.assertTrue(cfg.CAPTCHA_SOLVER_ENABLED)
        self.assertEqual(cfg.CAPTCHA_2CAPTCHA_KEY, "KEY-1")
        self.assertEqual(cfg.CAPTCHA_SOLVE_TIMEOUT, 180.0)
        self.assertEqual(cfg.CAPTCHA_POLL_INTERVAL, 5.0)
        self.assertTrue(cfg.VCC_AUTO_PAY_ENABLED)
        self.assertFalse(hasattr(cfg, "OTHER_SETTING"))

    def test_source_copies_captcha_prefix(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def _build_pay_cfg")
        seg = src[i:i + 1200]
        self.assertIn('startswith("CAPTCHA_")', seg)


class ExtractParamsTest(unittest.TestCase):
    """参数提取必须挑带 rqdata 的那个 sitekey。"""

    class _Frame:
        def __init__(self, url):
            self.url = url

    class _Page:
        def __init__(self, frames, url="https://pay.openai.com/p/session"):
            self.frames = frames
            self.url = url

        def evaluate(self, *a, **k):
            return "UA-1"

        def content(self):
            return ""

    def test_prefers_frame_with_rqdata(self):
        from core.vcc_pay_session import _extract_hcaptcha_params
        frames = [
            self._Frame("https://js.stripe.com/v3/hcaptcha-invisible-x.html"
                        "#sitekey=plain-sitekey&size=invisible"),
            self._Frame("https://js.stripe.com/v3/hcaptcha-inner-x.html"
                        "#sitekey=ent-sitekey&rqdata=ABC123&verifyUrl=Y"),
        ]
        p = _extract_hcaptcha_params(self._Page(frames))
        self.assertTrue(p["found"])
        self.assertEqual(p["sitekey"], "ent-sitekey")
        self.assertEqual(p["rqdata"], "ABC123")
        self.assertEqual(p["pageurl"], "https://pay.openai.com/p/session")
        self.assertEqual(p["userAgent"], "UA-1")

    def test_falls_back_to_plain_when_no_rqdata(self):
        from core.vcc_pay_session import _extract_hcaptcha_params
        frames = [self._Frame("https://js.stripe.com/v3/hcaptcha-invisible.html"
                              "#sitekey=only-sitekey")]
        p = _extract_hcaptcha_params(self._Page(frames))
        self.assertEqual(p["sitekey"], "only-sitekey")
        self.assertEqual(p["rqdata"], "")

    def test_no_hcaptcha_frame_reports_not_found(self):
        from core.vcc_pay_session import _extract_hcaptcha_params
        p = _extract_hcaptcha_params(self._Page([self._Frame("https://x/")]))
        self.assertFalse(p["found"])


if __name__ == "__main__":
    unittest.main()
