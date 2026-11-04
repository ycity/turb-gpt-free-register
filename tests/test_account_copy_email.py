# -*- coding: utf-8 -*-
"""账号表格邮箱后面的复制按钮。"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HTML = (Path(__file__).resolve().parents[1]
        / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


class CopyEmailButtonTest(unittest.TestCase):
    def test_button_follows_email_in_same_div(self):
        """必须和邮箱同一行、紧跟其后。"""
        i = HTML.index('<div class="acc-v2-email">')
        seg = HTML[i:HTML.index("</div>", i)]
        self.assertIn("acc-v2-copy-email", seg)
        self.assertLess(seg.index("${esc(r.email)}"), seg.index("acc-v2-copy-email"))

    def _button_tag(self) -> str:
        """取按钮标签本身——类名在 CSS 块里也出现，不能直接 index。"""
        i = HTML.index('<button type="button" class="acc-v2-copy-email"')
        return HTML[i:HTML.index("</button>", i)]

    def test_value_attribute_escaped(self):
        self.assertIn("attrEsc(r.email", self._button_tag())

    def test_handler_present(self):
        i = HTML.index("const copyEmailBtn")
        seg = HTML[i:i + 520]
        self.assertIn("copyText(value)", seg)
        self.assertIn("return;", seg)

    def test_no_server_roundtrip(self):
        """邮箱不是敏感字段，表格里就有，不该再去服务端取。"""
        i = HTML.index("const copyEmailBtn")
        seg = HTML[i:i + 520]
        self.assertNotIn("fetchOneAccountSecret", seg)
        self.assertIn("dataset.copyEmail", seg)

    def test_empty_email_handled(self):
        i = HTML.index("const copyEmailBtn")
        seg = HTML[i:i + 520]
        self.assertIn("该账号没有邮箱", seg)

    def test_button_cannot_be_pushed_to_next_line(self):
        """邮箱列开了 break-word，按钮必须不参与换行。"""
        i = HTML.index(".accounts-table-v2 .acc-v2-copy-email {")
        rule = HTML[i:HTML.index("}", i)]
        self.assertIn("white-space: nowrap", rule)
        self.assertIn("flex: none", rule)

    def test_hidden_until_row_hover(self):
        i = HTML.index(".accounts-table-v2 .acc-v2-copy-email {")
        rule = HTML[i:HTML.index("}", i)]
        self.assertIn("opacity: 0", rule)
        self.assertIn("tr:hover .acc-v2-copy-email", HTML)

    def test_keyboard_accessible(self):
        """悬停才显示的控件必须能被键盘聚焦看到。"""
        self.assertIn(".acc-v2-copy-email:focus-visible", HTML)
        self.assertIn("aria-label", self._button_tag())

    def test_click_feedback(self):
        """纯图标按钮没有文字可变，要有视觉反馈。"""
        self.assertIn(".acc-v2-copy-email.copied", HTML)
        i = HTML.index("const copyEmailBtn")
        self.assertIn("classList.add('copied')", HTML[i:i + 520])


if __name__ == "__main__":
    unittest.main()
