# -*- coding: utf-8 -*-
"""失效长链不该白等满超时。

实测：一条作废的长链，页面 0.66s 就加载完，1.5s 渲染出
"You're all done here"，但表单永远不出现。原先只在 wait_for_selector 超时
之后才去读这段文字，于是每次都要先空等 VCC_PAGE_TIMEOUT=60 秒。
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vcc_checkout_pay as vp  # noqa: E402

EXPIRED = ("You’re all done here\n\nYou’ve either completed your payment "
           "or this checkout session has timed out.")


class _Cfg:
    VCC_PAGE_TIMEOUT = 60.0
    VCC_FORM_TIMEOUT = 12.0


def _page(*, form_after=None, body_after=None, body=""):
    """form_after/body_after：第几次调用后才出现。"""
    page = MagicMock()
    state = {"waits": 0, "reads": 0}

    def wait(sel, timeout=None):
        state["waits"] += 1
        if form_after is not None and state["waits"] >= form_after:
            return MagicMock()
        raise RuntimeError("timeout")

    def read(sel, timeout=None):
        state["reads"] += 1
        if body_after is not None and state["reads"] >= body_after:
            return EXPIRED
        return body

    page.wait_for_selector.side_effect = wait
    page.inner_text.side_effect = read
    page._state = state
    return page


class FastFailTest(unittest.TestCase):
    def test_expired_detected_without_full_wait(self):
        """关键：页面文字一出现就失败，不等满 12 秒。"""
        page = _page(body_after=3)
        t0 = time.monotonic()
        with self.assertRaises(vp.VccPayError) as ctx:
            vp._wait_form_or_fail(page, _Cfg())
        spent = time.monotonic() - t0
        self.assertIn("已过期", str(ctx.exception))
        self.assertLess(spent, 5.0, "识别出过期后应立刻失败")

    def test_form_found_returns_immediately(self):
        page = _page(form_after=1)
        vp._wait_form_or_fail(page, _Cfg())
        self.assertEqual(page._state["waits"], 1)

    def test_slow_form_still_waits(self):
        """表单慢几轮才出现，不能因为查过期就提前放弃。"""
        page = _page(form_after=3)
        vp._wait_form_or_fail(page, _Cfg())
        self.assertGreaterEqual(page._state["waits"], 3)

    def test_blank_page_message_differs_from_expired(self):
        """空白页多半是代理问题，长链可能还有效，不该说成已作废。"""
        page = _page(body="")
        with patch.object(vp.time, "monotonic", side_effect=[0, 0, 99, 99, 99]):
            with self.assertRaises(vp.VccPayError) as ctx:
                vp._wait_form_or_fail(page, _Cfg())
        msg = str(ctx.exception)
        self.assertIn("空白", msg)
        self.assertNotIn("已作废", msg)

    def test_other_text_is_reported(self):
        page = _page(body="Something unexpected happened")
        with patch.object(vp.time, "monotonic", side_effect=[0, 0, 99, 99, 99]):
            with self.assertRaises(vp.VccPayError) as ctx:
                vp._wait_form_or_fail(page, _Cfg())
        self.assertIn("Something unexpected", str(ctx.exception))

    def test_expired_text_helper(self):
        page = MagicMock()
        page.inner_text.return_value = EXPIRED
        self.assertTrue(vp._expired_text(page))
        page.inner_text.return_value = "Pay with card"
        self.assertFalse(vp._expired_text(page))

    def test_unreadable_body_is_not_expired(self):
        """读不到正文只是还没渲染完，不能当成过期。"""
        page = MagicMock()
        page.inner_text.side_effect = RuntimeError("detached")
        self.assertFalse(vp._expired_text(page))


class TimeoutConfigTest(unittest.TestCase):
    def test_form_timeout_is_short(self):
        from config import vcc as cfg
        self.assertLessEqual(cfg.VCC_FORM_TIMEOUT, 30.0)
        self.assertLess(cfg.VCC_FORM_TIMEOUT, cfg.VCC_PAGE_TIMEOUT)

    def test_falls_back_to_page_timeout(self):
        class Old:
            VCC_PAGE_TIMEOUT = 60.0
        page = _page(form_after=1)
        vp._wait_form_or_fail(page, Old())

    def test_env_overridable(self):
        src = Path("config/vcc.py").read_text(encoding="utf-8")
        seg = src[src.index("apply_env_overrides"):]
        self.assertIn("'VCC_FORM_TIMEOUT': 'float'", seg)


if __name__ == "__main__":
    unittest.main()
