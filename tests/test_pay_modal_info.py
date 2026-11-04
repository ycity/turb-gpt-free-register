# -*- coding: utf-8 -*-
"""支付弹窗常驻显示账号邮箱和代理。

看服务端截图时经常分不清这一单是哪个号、走的哪个出口，尤其是上一单失败后
重新发起时。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HTML = (Path(__file__).resolve().parents[1]
        / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    m = re.search(r"function " + re.escape(name) + r"\s*\(", HTML)
    assert m, f"找不到 function {name}"
    i = m.start()
    return HTML[i:HTML.index("\nfunction ", i + 10)]


class InfoBarTest(unittest.TestCase):
    def test_bar_exists_in_modal(self):
        i = HTML.index('<div id="autoPayModal"')
        seg = HTML[i:i + 3000]
        self.assertIn('id="autoPayWho"', seg)

    def test_filled_on_open(self):
        self.assertIn("_autoPayRenderWho(id)", _fn("_autoPayOpenModal"))

    def test_shows_email_and_proxy(self):
        seg = _fn("_autoPayRenderWho")
        self.assertIn("acc.email", seg)
        self.assertIn("acc.proxy_used", seg)

    def test_proxy_credentials_masked(self):
        """常驻显示，绝不能把代理密码摊在屏幕上。"""
        seg = _fn("_autoPayMaskProxy")
        self.assertIn("hostname", seg)
        self.assertNotIn("u.password", seg)
        self.assertNotIn("u.username", seg)

    def test_direct_connection_labeled(self):
        self.assertIn("直连", _fn("_autoPayMaskProxy"))

    def test_values_escaped(self):
        seg = _fn("_autoPayRenderWho")
        self.assertIn("esc(email)", seg)
        self.assertIn("esc(proxy)", seg)

    def test_render_does_not_clobber_bar(self):
        """支付过程中反复重绘，信息条必须一直在。"""
        self.assertNotIn("autoPayWho", _fn("_autoPayRender"))

    def test_hidden_when_no_account(self):
        seg = _fn("_autoPayRenderWho")
        self.assertIn("classList.add('hidden')", seg)

    def test_styled(self):
        self.assertIn(".ap-who {", HTML)


if __name__ == "__main__":
    unittest.main()
