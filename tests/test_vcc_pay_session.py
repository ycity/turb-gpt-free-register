# -*- coding: utf-8 -*-
"""会话式支付状态机测试。

用假 page / 假 VCC client，不开浏览器、不连网、不动钱。
"""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.vcc_pay_session as ps  # noqa: E402


class FakeVcc:
    def pick_card(self, **_kw):
        return {"cardId": "TEST", "cardNoMask": "555659****8361"}

    def pick_cardholder(self, _hid):
        return {"holderName": "ONeal Di"}

    def card_secrets(self, _cid):
        return {"number": "5556597902158361", "exp_month": "10",
                "exp_year": "28", "cvc": "075"}

    def wait_3ds_code(self, *_a, **_kw):
        return ""


def _cfg(**over):
    from config import vcc as base

    class Cfg:
        pass

    for name in dir(base):
        if name.startswith("VCC_"):
            setattr(Cfg, name, getattr(base, name))
    Cfg.VCC_SESSION_TIMEOUT = 3.0
    Cfg.VCC_3DS_ENABLED = False
    for key, value in over.items():
        setattr(Cfg, key, value)
    return Cfg()


class SessionStateTest(unittest.TestCase):
    def setUp(self):
        ps._sessions.clear()

    def tearDown(self):
        ps._sessions.clear()

    def _session(self, state=ps.ST_AWAITING_CONFIRM, **kw):
        sess = ps.PaySession(sid="t1", account_id=1, email="a@b.c", state=state)
        for key, value in kw.items():
            setattr(sess, key, value)
        ps._sessions[sess.sid] = sess
        return sess

    def test_can_confirm_only_without_gate_error(self):
        ok = self._session()
        self.assertTrue(ok.public()["can_confirm"])
        blocked = self._session(gate_error="金额不符")
        self.assertFalse(blocked.public()["can_confirm"])

    def test_public_view_has_no_plaintext_card(self):
        """对外视图只能有掩码，不能泄露卡号明文。"""
        view = self._session(card_mask="555659****8361").public()
        blob = repr(view)
        self.assertIn("555659****8361", blob)
        self.assertNotIn("5556597902158361", blob)
        self.assertNotIn("075", view.get("card_mask", ""))

    def test_send_command_refused_after_terminal(self):
        sess = self._session(state=ps.ST_DONE)
        self.assertFalse(ps.send_command(sess.sid, "confirm"))

    def test_only_one_live_session(self):
        self._session()
        with self.assertRaises(RuntimeError):
            ps.start_session(account_id=2, email="x@y.z",
                             checkout_url="http://localhost/none",
                             cfg=_cfg(), client=FakeVcc())

    def test_terminal_session_frees_the_slot(self):
        """终态会话不应再占用唯一名额。"""
        self._session(state=ps.ST_CANCELLED)
        started = {}

        def fake_worker(sess, url, proxy, cfg, client):
            started["sid"] = sess.sid
            started["proxy"] = proxy
            sess.state = ps.ST_CANCELLED

        orig = ps._worker
        ps._worker = fake_worker
        try:
            sess = ps.start_session(account_id=2, email="x@y.z",
                                    checkout_url="http://example.invalid/none",
                                    proxy="http://u:p@proxy.test:8080",
                                    cfg=_cfg(), client=FakeVcc())
            sess._thread.join(timeout=3)
        except RuntimeError as exc:
            self.fail(f"终态会话仍占用名额：{exc}")
        finally:
            ps._worker = orig
        self.assertEqual(started.get("sid"), sess.sid)

    def test_proxy_is_passed_through_to_worker(self):
        """支付必须走账号注册时的代理，代理参数不能在半路丢掉。"""
        seen = {}

        def fake_worker(sess, url, proxy, cfg, client):
            seen["proxy"] = proxy
            seen["url"] = url
            sess.state = ps.ST_CANCELLED

        orig = ps._worker
        ps._worker = fake_worker
        try:
            sess = ps.start_session(account_id=3, email="p@q.r",
                                    checkout_url="http://example.invalid/pay",
                                    proxy="http://user:pass@tw.example:6611",
                                    cfg=_cfg(), client=FakeVcc())
            sess._thread.join(timeout=3)
        finally:
            ps._worker = orig
        self.assertEqual(seen.get("proxy"), "http://user:pass@tw.example:6611")
        self.assertEqual(seen.get("url"), "http://example.invalid/pay")

    def test_reap_drops_only_old_terminal(self):
        old = self._session(state=ps.ST_DONE)
        old.updated_at = time.time() - 7200
        live = ps.PaySession(sid="t2", account_id=2,
                             state=ps.ST_AWAITING_CONFIRM)
        live.updated_at = time.time() - 7200
        ps._sessions[live.sid] = live
        ps._reap(max_age=3600)
        self.assertNotIn("t1", ps._sessions)
        self.assertIn("t2", ps._sessions)

    def test_last_finished_returns_recent_terminal(self):
        """刚付完刷新页面还要能看到结果，不能只认进行中的会话。"""
        done = self._session(state=ps.ST_DONE)
        done.updated_at = time.time()
        self.assertIsNotNone(ps.last_finished())
        self.assertEqual(ps.last_finished().sid, done.sid)

    def test_last_finished_ignores_stale(self):
        old = self._session(state=ps.ST_DONE)
        old.updated_at = time.time() - 3600
        self.assertIsNone(ps.last_finished(max_age=600))

    def test_last_finished_picks_newest(self):
        a = self._session(state=ps.ST_DONE)
        a.updated_at = time.time() - 100
        b = ps.PaySession(sid="t9", account_id=1, state=ps.ST_FAILED)
        b.updated_at = time.time()
        ps._sessions[b.sid] = b
        self.assertEqual(ps.last_finished().sid, "t9")


class WaitAndSubmitTest(unittest.TestCase):
    """确认前后的行为：闸门、金额复核、取消。"""

    class FakePage:
        def __init__(self, total=("EGP", 1150.0)):
            self.total = total
            self.clicked = []
            self.frames = []

        def click(self, sel):
            self.clicked.append(sel)

        def query_selector(self, _sel):
            # 点过提交就当作页面已离开表单，模拟跳转
            return None if self.clicked else object()

        def inner_text(self, _sel, timeout=0):
            return ""

        def wait_for_timeout(self, _ms):
            pass

        def screenshot(self, **_kw):
            return b"\x89PNG\r\n\x1a\n"

    def setUp(self):
        ps._sessions.clear()
        self._orig_read = ps.read_total_safe

    def tearDown(self):
        ps.read_total_safe = self._orig_read
        ps._sessions.clear()

    def _run(self, sess, page, cfg, amount=1150.0, currency="EGP"):
        t = threading.Thread(
            target=ps._wait_and_submit,
            args=(page, sess, cfg, FakeVcc(), "CARD", Path("."),
                  currency, amount, "button#submit", lambda _p: ""),
            daemon=True)
        t.start()
        return t

    def test_gate_error_blocks_submit(self):
        """闸门未通过时即使收到确认也绝不点提交。"""
        sess = ps.PaySession(sid="g", account_id=1,
                             state=ps.ST_AWAITING_CONFIRM,
                             gate_error="应付金额 EGP 1311.00 不等于期望 1150.00")
        page = self.FakePage()
        ps.read_total_safe = lambda _p: ("EGP", 1311.0)
        t = self._run(sess, page, _cfg())
        sess._cmd.put({"op": "confirm"})
        t.join(timeout=5)
        self.assertEqual(page.clicked, [])
        self.assertFalse(sess.submitted)

    def test_amount_change_between_view_and_confirm_blocks_submit(self):
        """看到的和提交的必须是同一个数：期间页面变价就拒绝。"""
        sess = ps.PaySession(sid="c", account_id=1,
                             state=ps.ST_AWAITING_CONFIRM)
        page = self.FakePage()
        ps.read_total_safe = lambda _p: ("EGP", 2300.0)
        t = self._run(sess, page, _cfg(), amount=1150.0)
        sess._cmd.put({"op": "confirm"})
        t.join(timeout=5)
        self.assertEqual(page.clicked, [])
        self.assertEqual(sess.state, ps.ST_FAILED)
        self.assertIn("金额已变化", sess.message)

    def test_confirm_submits_when_everything_matches(self):
        sess = ps.PaySession(sid="k", account_id=1,
                             state=ps.ST_AWAITING_CONFIRM)
        page = self.FakePage()
        ps.read_total_safe = lambda _p: ("EGP", 1150.0)
        t = self._run(sess, page, _cfg())
        sess._cmd.put({"op": "confirm"})
        t.join(timeout=5)
        self.assertEqual(page.clicked, ["button#submit"])
        self.assertTrue(sess.submitted)
        self.assertEqual(sess.state, ps.ST_DONE)

    def test_cancel_never_submits(self):
        sess = ps.PaySession(sid="x", account_id=1,
                             state=ps.ST_AWAITING_CONFIRM)
        page = self.FakePage()
        ps.read_total_safe = lambda _p: ("EGP", 1150.0)
        t = self._run(sess, page, _cfg())
        sess._cmd.put({"op": "cancel"})
        t.join(timeout=5)
        self.assertEqual(page.clicked, [])
        self.assertEqual(sess.state, ps.ST_CANCELLED)

    def test_timeout_never_submits(self):
        sess = ps.PaySession(sid="t", account_id=1,
                             state=ps.ST_AWAITING_CONFIRM)
        page = self.FakePage()
        ps.read_total_safe = lambda _p: ("EGP", 1150.0)
        t = self._run(sess, page, _cfg(VCC_SESSION_TIMEOUT=1.0))
        t.join(timeout=6)
        self.assertEqual(page.clicked, [])
        self.assertEqual(sess.state, ps.ST_FAILED)
        self.assertIn("超时", sess.message)


class Find3dsFrameTest(unittest.TestCase):
    """区分真 3DS 质询和 Stripe 的隐藏预热容器。

    只看 frame 名字会把 Processing 误判成需要人工输码，支付就此卡死。
    """

    class FakeLocator:
        def __init__(self, visible):
            self._visible = visible

        @property
        def first(self):
            return self

        def is_visible(self, timeout=0):
            return self._visible

    class FakeFrame:
        def __init__(self, name="", url="", visible_input=False):
            self.name = name
            self.url = url
            self._visible = visible_input

        def locator(self, _sel):
            return Find3dsFrameTest.FakeLocator(self._visible)

    class FakePage:
        def __init__(self, frames):
            self.frames = frames

    def test_hidden_prewarm_frame_is_not_a_challenge(self):
        page = self.FakePage([
            self.FakeFrame(name="__privateStripeFrame3ds",
                           url="https://js.stripe.com/v3/three-ds-2.html",
                           visible_input=False),
        ])
        frame, sel = ps._find_3ds_frame(page)
        self.assertIsNone(frame)

    def test_visible_challenge_input_is_detected(self):
        page = self.FakePage([
            self.FakeFrame(name="other", url="https://js.stripe.com/x.html"),
            self.FakeFrame(name="challengeFrame",
                           url="https://acs.example.com/challenge",
                           visible_input=True),
        ])
        frame, sel = ps._find_3ds_frame(page)
        self.assertIsNotNone(frame)
        self.assertTrue(sel)

    def test_unrelated_frames_ignored(self):
        page = self.FakePage([
            self.FakeFrame(name="hcaptcha-invisible",
                           url="https://newassets.hcaptcha.com/captcha/v1/x",
                           visible_input=True),
        ])
        frame, _ = ps._find_3ds_frame(page)
        self.assertIsNone(frame)


class WaitSettledTest(unittest.TestCase):
    """提交后不能固定等几秒就下结论。"""

    class Page:
        """按顺序吐出提交按钮文案；用完后一直保持最后一个状态。"""

        def __init__(self, labels, frames=None):
            self._labels = list(labels)
            self.frames = frames or []

        def _label(self):
            if len(self._labels) > 1:
                return self._labels.pop(0)
            return self._labels[0] if self._labels else ""

        def query_selector(self, _sel):
            return object()

        def inner_text(self, _sel, timeout=0):
            return self._label()

        def wait_for_timeout(self, _ms):
            pass

    def test_processing_then_left_form_counts_as_left(self):
        page = self.Page(["Processing", "Processing", "Subscribe"])
        self.assertEqual(ps._wait_settled(page, None, timeout=10), "left")

    def test_still_processing_reports_unknown(self):
        """一直处理中必须报告结果未知，绝不能当成成功。"""
        page = self.Page(["Processing"])
        self.assertEqual(ps._wait_settled(page, None, timeout=2), "processing")

    def test_form_gone_means_left(self):
        class Gone(self.Page):
            def query_selector(self, _sel):
                return None

        self.assertEqual(ps._wait_settled(Gone(["x"]), None, timeout=5), "left")


class CaptchaDetectTest(unittest.TestCase):
    """识别 hCaptcha。

    hCaptcha 的答案绑定在解题的那个浏览器上，没法在别处解完搬过来，
    所以必须准确识别并把页面交给人直接操作。
    """

    class Loc:
        def __init__(self, visible):
            self._v = visible

        @property
        def first(self):
            return self

        def is_visible(self, timeout=0):
            return self._v

    class Frame:
        def __init__(self, url="", visible=False):
            self.url = url
            self.name = ""
            self._v = visible

        def locator(self, _s):
            return CaptchaDetectTest.Loc(self._v)

    class Page:
        def __init__(self, frames, texts=()):
            self.frames = frames
            self._texts = set(texts)

        def get_by_text(self, text, exact=False):
            return CaptchaDetectTest.Loc(text in self._texts)

    def test_hcaptcha_frame_detected(self):
        page = self.Page([self.Frame("https://newassets.hcaptcha.com/captcha/v1/x",
                                     visible=True)])
        self.assertTrue(ps._detect_captcha(page))

    def test_stripe_prompt_text_detected(self):
        page = self.Page([], texts={"One more step before you're done"})
        self.assertTrue(ps._detect_captcha(page))

    def test_ordinary_page_is_not_captcha(self):
        page = self.Page([self.Frame("https://js.stripe.com/v3/controller.html",
                                     visible=True)])
        self.assertFalse(ps._detect_captcha(page))


class ApplyInputTest(unittest.TestCase):
    """人在网页上的操作要原样转发给服务端浏览器。"""

    class Mouse:
        def __init__(self, log):
            self.log = log

        def click(self, x, y):
            self.log.append(("click", x, y))

        def move(self, x, y):
            self.log.append(("move", x, y))

        def down(self):
            self.log.append(("down",))

        def up(self):
            self.log.append(("up",))

        def wheel(self, dx, dy):
            self.log.append(("wheel", dx, dy))

    class Keyboard:
        def __init__(self, log):
            self.log = log

        def press(self, key):
            self.log.append(("press", key))

        def type(self, text):
            self.log.append(("type", text))

    class Page:
        def __init__(self):
            self.log = []
            self.mouse = ApplyInputTest.Mouse(self.log)
            self.keyboard = ApplyInputTest.Keyboard(self.log)

    def test_click_forwarded_with_coords(self):
        page = self.Page()
        ps._apply_input(page, {"kind": "click", "x": 120, "y": 340})
        self.assertEqual(page.log, [("click", 120.0, 340.0)])

    def test_text_and_key_forwarded(self):
        page = self.Page()
        ps._apply_input(page, {"kind": "text", "text": "abc"})
        ps._apply_input(page, {"kind": "key", "key": "Enter"})
        self.assertEqual(page.log, [("type", "abc"), ("press", "Enter")])

    def test_unknown_kind_is_ignored(self):
        page = self.Page()
        ps._apply_input(page, {"kind": "teleport", "x": 1})
        self.assertEqual(page.log, [])

    def test_broken_page_does_not_raise(self):
        class Bad:
            @property
            def mouse(self):
                raise RuntimeError("frame detached")

        ps._apply_input(Bad(), {"kind": "click", "x": 1, "y": 2})


class LaunchBrowserTest(unittest.TestCase):
    """选浏览器：优先 CloakBrowser，失败要能退回裸 Playwright。

    裸 Chromium 会被 Stripe 风控和发卡行 3DS 判成自动化环境，所以默认走
    CloakBrowser；但它不可用时不能让支付直接崩。
    """

    class Sess:
        sid = "t"

    class FakePw:
        def __init__(self):
            self.called = None

        @property
        def chromium(self):
            return self

        def launch(self, **kw):
            self.called = kw
            return "plain-browser"

    def test_uses_cloak_when_available(self):
        captured = {}

        def fake_launch(**kw):
            captured.update(kw)
            return "cloak-browser"

        import sys as _sys
        import types
        mod = types.ModuleType("cloakbrowser")
        mod.launch = fake_launch
        old = _sys.modules.get("cloakbrowser")
        _sys.modules["cloakbrowser"] = mod
        try:
            browser = ps._try_cloak_browser(
                _cfg(VCC_USE_CLOAK=True, VCC_SERVER_HEADED=True),
                "http://u:p@h:1", self.Sess())
        finally:
            if old is None:
                _sys.modules.pop("cloakbrowser", None)
            else:
                _sys.modules["cloakbrowser"] = old
        self.assertEqual(browser, "cloak-browser")
        self.assertTrue(captured.get("humanize"))
        self.assertTrue(captured.get("geoip"))
        self.assertFalse(captured.get("headless"))

    def test_falls_back_when_cloak_launch_raises(self):
        import sys as _sys
        import types
        mod = types.ModuleType("cloakbrowser")

        def boom(**_kw):
            raise RuntimeError("Sync API inside the asyncio loop")

        mod.launch = boom
        old = _sys.modules.get("cloakbrowser")
        _sys.modules["cloakbrowser"] = mod
        try:
            browser = ps._try_cloak_browser(
                _cfg(VCC_USE_CLOAK=True), "", self.Sess())
        finally:
            if old is None:
                _sys.modules.pop("cloakbrowser", None)
            else:
                _sys.modules["cloakbrowser"] = old
        self.assertIsNone(browser)

    def test_disabled_switch_skips_cloak(self):
        self.assertIsNone(
            ps._try_cloak_browser(_cfg(VCC_USE_CLOAK=False), "", self.Sess()))

    def test_plain_path_splits_proxy_credentials(self):
        pw = self.FakePw()
        ps._plain_browser(pw, _cfg(), "http://user:pw@host:8080")
        proxy = pw.called.get("proxy") or {}
        self.assertEqual(proxy.get("server"), "http://host:8080")
        self.assertEqual(proxy.get("username"), "user")


if __name__ == "__main__":
    unittest.main(verbosity=2)
