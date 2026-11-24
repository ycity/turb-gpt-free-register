# -*- coding: utf-8 -*-
"""人工接管画面的远程操作能力。

人机验证里有「把图块拖到空位」这类题，只发 click 永远过不了，
必须把 mousedown / mousemove / mouseup 三段都转发给服务端浏览器。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
SESSION = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")


def _bind_block() -> str:
    i = HTML.index("function _autoPayBindLive")
    return HTML[i:i + 3400]


class BackendInputTest(unittest.TestCase):
    def test_route_accepts_drag_kinds(self):
        idx = APP.index("def api_pay_input")
        body = APP[idx:idx + 900]
        for kind in ("down", "move", "up"):
            self.assertIn(f'"{kind}"', body, f"/input 不接受 {kind}")

    def test_worker_implements_drag(self):
        """down/up 必须先 move 再按，否则按在上一个坐标上。"""
        idx = SESSION.index('elif kind == "down"')
        body = SESSION[idx:idx + 400]
        self.assertIn("page.mouse.move", body)
        self.assertIn("page.mouse.down()", body)
        up = SESSION[SESSION.index('elif kind == "up"'):][:300]
        self.assertIn("page.mouse.move", up)
        self.assertIn("page.mouse.up()", up)


class FrontendDragTest(unittest.TestCase):
    def test_sends_all_three_phases(self):
        body = _bind_block()
        for kind in ("'down'", "'move'", "'up'"):
            self.assertIn(f"_autoPaySend({kind}", body, f"前端没发 {kind}")

    def test_binds_mouse_events(self):
        body = _bind_block()
        for ev in ("mousedown", "mousemove", "mouseup"):
            self.assertIn(f"'{ev}'", body, f"没绑定 {ev}")

    def test_mouseup_on_document_not_image(self):
        """拖出图片边界时在 img 上收不到 mouseup，会一直卡在按下状态。"""
        body = _bind_block()
        idx = body.index("'mouseup'")
        self.assertIn("document.addEventListener", body[max(0, idx - 120):idx])

    def test_move_is_throttled(self):
        """不节流的话一秒上百条指令会把后端队列堵死。"""
        body = _bind_block()
        self.assertIn("MOVE_INTERVAL_MS", body)

    def test_click_still_works_for_tap(self):
        """多数验证题是点选，不能因为加了拖拽就把普通点击吞掉。"""
        body = _bind_block()
        self.assertIn("_autoPaySend('click'", body)
        self.assertIn("movedWhileDown", body)

    def test_click_suppressed_only_after_real_drag(self):
        """手抖几像素不该被当成拖拽，否则点选会失灵。"""
        body = _bind_block()
        self.assertIn("DRAG_SLOP_PX", body)

    def test_default_image_drag_prevented(self):
        """不拦的话浏览器会把截图本身拖起来，根本到不了我们的逻辑。"""
        body = _bind_block()
        self.assertIn("ev.preventDefault()", body)
        self.assertIn('draggable="false"', HTML)

    def test_up_coords_clamped(self):
        """拖出边界时坐标会越界，落点必须夹回视口内。"""
        body = _bind_block()
        idx = body.index("_autoPaySend('up'")
        self.assertIn("Math.min(_autoPayViewport", body[max(0, idx - 400):idx])

    def test_only_left_button_starts_drag(self):
        body = _bind_block()
        self.assertIn("ev.button !== 0", body)


class VncFallbackTest(unittest.TestCase):
    """截图模式每 700ms 才刷一帧，轨迹类验证仍建议走 VNC。"""

    def test_vnc_tip_present(self):
        self.assertIn("autoPayVncTip", HTML)
        self.assertIn("vnc.html", HTML)

    def test_vnc_status_route(self):
        self.assertIn("/api/pay/vnc-status", APP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
