# -*- coding: utf-8 -*-
"""账号表：密码与 2FA 合并、操作列主位换成「订阅Team」。

改动意图：
  - 密码单独占一列太浪费，合进 2FA 列
  - 「已启用」徽章是冗余的：有「复制密钥」「看验证码」就说明启用了
  - 「订阅Team」是高频操作，不该埋在更多菜单里
  - 「删除」不可逆，不该放在主位容易误点
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


def _table():
    i = HTML.index('id="accountsSelectAllV2"')
    return HTML[HTML.rindex("<colgroup", 0, i):HTML.index("</thead>", i)]


def _row():
    i = HTML.index("const rowHtmlV2 = (r) =>")
    return HTML[i:HTML.index("`;", i)]


def _more_menu():
    i = HTML.index("function _accountsV2MoreMenu")
    return HTML[i:HTML.index("function closeAccountsV2MoreMenus")]


def _totp_cell():
    i = HTML.index("function _totpCellV2")
    return HTML[i:HTML.index("function _codexAgentCell")]


class ColumnTest(unittest.TestCase):
    def test_password_column_removed(self):
        """密码列已合进 2FA 列，残留的 col-password 会让人以为还在。"""
        self.assertNotIn("col-password", HTML)

    def test_header_renamed(self):
        self.assertIn("密码 / 2FA", _table())

    def test_counts_match(self):
        """colgroup / thead / 数据行 / colspan 四处必须一致，否则表格错位。"""
        head = _table()
        cols = len(re.findall(r"<col ", head))
        ths = len(re.findall(r"<th ", head))
        tds = len(re.findall(r"^\s*<td ", _row(), re.M))
        i = HTML.index("暂无账号")
        m = re.search(r'colspan="(\d+)"', HTML[max(0, i - 200):i])
        self.assertEqual(cols, ths, "colgroup 与表头不一致")
        self.assertEqual(ths, tds, "表头与数据行不一致")
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), cols, "colspan 与列数不一致")

    def test_merged_column_is_wide_enough(self):
        """这一列要放 4 个按钮（复制密码/复制密钥/看验证码/席位标）。

        实测 280px 仍会挤成两行，330px 才够一行放下。
        """
        m = re.search(r"\.accounts-table-v2 \.col-small \{ width: (\d+)px", HTML)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 320)

    def test_merged_column_wraps(self):
        """按钮多，必须允许换行，否则会被截断。"""
        i = HTML.index("white-space: normal; overflow: visible")
        seg = HTML[max(0, i - 400):i]
        self.assertIn(".accounts-table-v2 .col-small", seg)


class PasswordButtonTest(unittest.TestCase):
    def test_copy_password_button_exists(self):
        self.assertIn('data-account-copy-secret="password"', _totp_cell())

    def test_uses_existing_secret_mechanism(self):
        """复用 data-account-copy-secret：密码按需从后端取，不渲染进 DOM。"""
        cell = _totp_cell()
        self.assertNotIn("esc(r.password)", cell)
        self.assertIn("r.password", cell)   # 只用来判断有没有

    def test_backend_supports_password_field(self):
        app = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = app.index("def _account_secret_value")
        self.assertIn("password", app[i:i + 2000])

    def test_toast_has_password_case(self):
        """不加这条分支，复制密码会提示"账号整行已复制"。"""
        i = HTML.index("field === 'totp_secret' ? '2FA 密钥已复制'")
        self.assertIn("field === 'password'", HTML[i:i + 300])

    def test_shown_in_all_states(self):
        """密码和 2FA 状态无关，三个分支都要能复制。"""
        cell = _totp_cell()
        self.assertEqual(cell.count("${pwdBtn}"), 3)

    def test_handles_missing_password(self):
        self.assertIn("无密码", _totp_cell())


class BadgeRemovedTest(unittest.TestCase):
    def test_enabled_badge_gone(self):
        """有「复制密钥」和「看验证码」就说明启用了，徽章纯占地方。

        只检查渲染出的徽章元素，不能搜中文字面量——
        解释这次改动的注释里也会出现这个词。
        """
        cell = _totp_cell()
        self.assertNotIn("acc-v2-twofa-badge", cell)
        self.assertNotIn(">已启用<", cell)

    def test_tooltip_preserved(self):
        """徽章去掉了，但它携带的 2FA 提示信息不能跟着丢。"""
        cell = _totp_cell()
        self.assertIn("keyTitle", cell)
        self.assertIn("titleText", cell)


class ActionLayoutTest(unittest.TestCase):
    def test_subscribe_in_primary_slot(self):
        i = _row().index("col-actions")
        act = _row()[i:]
        self.assertIn("订阅Team", act)
        self.assertIn("openAutoPay", act)

    def test_delete_moved_into_menu(self):
        """删除不可逆，不该和常用操作并排。"""
        i = _row().index("col-actions")
        self.assertNotIn("data-account-delete", _row()[i:])
        self.assertIn("data-account-delete", _more_menu())

    def test_delete_is_last_and_danger(self):
        menu = _more_menu()
        self.assertIn('class="danger" data-account-delete', menu)
        # 删除要排在归档之后，即菜单末尾
        self.assertGreater(menu.index("data-account-delete"),
                           menu.index("data-account-archive"))

    def test_old_entry_removed_from_menu(self):
        """移出来了就不该在菜单里留一份，否则两个入口做同一件事。"""
        self.assertNotIn("生成Team长链并支付", _more_menu())

    def test_plain_link_entry_kept(self):
        """「生成Team长链」是只提链不支付，和订阅是两件事，要保留。"""
        self.assertIn("生成Team长链<", _more_menu())

    def test_danger_style_exists(self):
        """菜单里的 danger 样式必须真实存在，否则红色标记无效。"""
        self.assertIn(".accounts-table-v2 .acc-v2-more-menu button.danger",
                      HTML)

    def test_actions_column_wide_enough(self):
        """「订阅Team」比「删除」宽，列宽不够会换行挤到两排。"""
        m = re.search(r"\.accounts-table-v2 \.col-actions \{ width: (\d+)px",
                      HTML)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 180)


class CodexBadgeTest(unittest.TestCase):
    """Codex 状态的视觉区分度。

    原来只有纯文字 + 颜色，「已通过」#377DFF 和「已跳过」#79bbff
    都是蓝色、只差深浅，扫一眼分不开。改成带底色和边框的徽章。
    """

    @staticmethod
    def _css():
        i = HTML.index(".accounts-table-v2 .acc-v2-codex {")
        return HTML[i:i + 1400]

    def test_is_badge_not_plain_text(self):
        css = self._css()
        self.assertIn("border-radius: 999px", css)
        self.assertIn("background:", css)

    def test_pass_and_skip_differ_in_hue(self):
        """两者必须不同色系，不能靠深浅区分。"""
        css = self._css()
        ok = re.search(r"\.acc-v2-codex\.is-ok \{\s*color: (#[0-9a-f]{6});"
                       r"\s*background: (#[0-9a-f]{6})", css, re.I)
        skip = re.search(r"\.acc-v2-codex\.is-skip \{\s*color: (#[0-9a-f]{6});"
                         r"\s*background: (#[0-9a-f]{6})", css, re.I)
        self.assertIsNotNone(ok, "找不到 is-ok 配色")
        self.assertIsNotNone(skip, "找不到 is-skip 配色")
        self.assertNotEqual(ok.group(1).lower(), skip.group(1).lower())
        self.assertNotEqual(ok.group(2).lower(), skip.group(2).lower())

    def test_every_state_has_background(self):
        """五个状态都要有底色，漏一个那个状态就还是纯文字。"""
        css = self._css()
        for st in ("is-ok", "is-skip", "is-fail", "is-run", "is-mute"):
            m = re.search(rf"\.acc-v2-codex\.{st} \{{[^}}]*background:", css)
            self.assertIsNotNone(m, f"{st} 没有底色")

    def test_scoped_to_accounts_table(self):
        """样式必须限定在账号表，别影响其它表。"""
        css = self._css()
        for st in ("is-ok", "is-skip", "is-fail"):
            self.assertIn(f".accounts-table-v2 .acc-v2-codex.{st}", css)

    def test_status_column_fits_badge(self):
        """徽章比纯文字宽，列太窄会被截断。"""
        m = re.search(r"\.accounts-table-v2 \.col-status \{ width: (\d+)px",
                      HTML)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 110)

    def test_proxy_column_width(self):
        m = re.search(r"\.accounts-table-v2 \.col-proxy \{ width: (\d+)px",
                      HTML)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 120)


class TimeColumnMergedTest(unittest.TestCase):
    """建号时间和下次账单合成一格两行。

    两个时间各占一列太占地方，而且它们总是一起看
    （这号什么时候建的、什么时候该续费）。
    """

    def test_single_time_column(self):
        """合并后只应剩一列 col-time。"""
        head = _table()
        self.assertEqual(head.count('<col class="col-time">'), 1)
        self.assertEqual(len(re.findall(r'<th class="col-time"', head)), 1)

    def test_header_renamed(self):
        head = _table()
        self.assertIn(">时间<", head)
        self.assertNotIn("下次账单<", head)
        self.assertNotIn("创建时间<", head)

    def test_cell_renders_two_lines(self):
        i = HTML.index("function _timeCellV2")
        body = HTML[i:HTML.index("function _discountLabel")]
        self.assertIn("建号", body)
        self.assertIn("账单", body)
        self.assertEqual(body.count("acc-v2-time-line"), 3)  # 1 定义 + 2 行

    def test_row_uses_merged_cell(self):
        row = _row()
        self.assertIn("_timeCellV2(r)", row)
        self.assertNotIn("_renewAtCell", row)

    def test_old_cell_fn_removed(self):
        """旧函数要么删掉要么改名，留着会有人以为还在用。"""
        self.assertNotIn("function _renewAtCell", HTML)

    def test_created_time_still_sgt(self):
        """合并不能把时区转换弄丢。"""
        i = HTML.index("function _timeCellV2")
        body = HTML[i:HTML.index("function _discountLabel")]
        self.assertIn("_fmtSgt(r.created_at)", body)

    def test_expiry_warning_preserved(self):
        """快到期/已过期的提醒不能因为改样式丢掉。"""
        i = HTML.index("function _renewAtValue")
        body = HTML[i:HTML.index("function _timeCellV2")]
        self.assertIn("is-expired", body)
        self.assertIn("is-soon", body)
        self.assertIn("days <= 3", body)

    def test_warning_styles_exist(self):
        """用到的 class 必须真有样式，否则告警看不出来。"""
        for cls in ("is-soon", "is-expired"):
            self.assertIn(f".acc-v2-time-line.{cls} .acc-v2-time-v", HTML)

    def test_tooltip_preserved(self):
        """扣款时刻、权益到期、剩余天数这些信息不能丢。"""
        i = HTML.index("function _renewAtValue")
        body = HTML[i:HTML.index("function _timeCellV2")]
        self.assertIn("下次扣款", body)
        self.assertIn("权益到期", body)
        self.assertIn("还有", body)

    def test_column_wide_enough_for_two_lines(self):
        """两行带标签，比原来单行宽。"""
        m = re.search(r"\.accounts-table-v2 \.col-time \{ width: (\d+)px",
                      HTML)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 200)

    def test_label_width_fixed(self):
        """标签定宽，两行的时间才能左对齐成一列。"""
        i = HTML.index(".accounts-table-v2 .acc-v2-time-k")
        self.assertIn("width:", HTML[i:i + 160])


if __name__ == "__main__":
    unittest.main(verbosity=2)
