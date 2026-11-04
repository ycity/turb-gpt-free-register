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
        self.assertIn("acc.register_proxy_name", seg)

    def test_uses_field_the_api_actually_returns(self):
        """列表接口不下发 proxy_used（含代理密码），读它只会拿到 undefined，
        结果所有账号都显示成"直连"。注释里提到它没关系，代码里不能再读。"""
        code = "\n".join(
            ln for ln in _fn("_autoPayRenderWho").splitlines()
            if not ln.lstrip().startswith("//"))
        self.assertNotIn("acc.proxy_used", code)

    def test_no_credentials_in_output(self):
        """代理名是池里的别名，天然不含密码。"""
        seg = _fn("_autoPayRenderWho")
        for leak in ("password", "username", "@"):
            self.assertNotIn(f"acc.{leak}", seg)

    def test_missing_proxy_labeled_without_claiming_direct(self):
        """拿不到代理名时别断言是直连——也可能是代理已从池中删除。"""
        seg = _fn("_autoPayRenderWho")
        self.assertIn("未绑定代理", seg)

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
