# -*- coding: utf-8 -*-
"""席位徽章要和 2FA 解耦（界面层）。

线上问题（2026-11-17）：
  关掉自动 2FA 后，#103 #104 #106 都已探出"支持 1 席"，接口也确实下发了
  single_seat_supported=True，但界面那一列只显示「复制密码 / 开启2FA」，
  席位徽章根本不出现。

根因：_totpCellV2 里 _singleSeatTag(r) 只写在 `if (enabled)` 分支中，
而 enabled = !!r.totp_enabled。没开 2FA 的号走的是最后一个 return，
整条分支压根没拼席位徽章。

这是和后端那处（席位探测只挂在 2FA 成功后）一模一样的耦合错误，
只是发生在渲染层。

对照实验式断言：同一份数据，只改 totp_enabled，两边都必须出现席位。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


def _fn(name):
    i = HTML.index("function %s(" % name)
    depth, j, started = 0, i, False
    while j < len(HTML):
        if HTML[j] == "{":
            depth += 1
            started = True
        elif HTML[j] == "}":
            depth -= 1
            if started and depth == 0:
                return HTML[i:j + 1]
        j += 1
    raise AssertionError("找不到函数 %s" % name)


def _returns(src):
    """抠出函数里所有 return 语句（到行尾）。"""
    return [m.group(0) for m in re.finditer(r"return `[^`]*`;", src)]


class SeatColumnDecoupledFrom2FATest(unittest.TestCase):
    """席位和首月免费独立成「Team席位」列，彻底与 2FA 列解耦。

    以前席位挤在 _totpCellV2 里导致未开 2FA 分支漏渲染；现在独立成
    <td class="col-seat">${_teamSeatCellV2(r)}</td>，无论 2FA 处于何种状态
    每行都会无条件渲染席位列。
    """

    def test_dedicated_team_seat_column_in_row(self):
        i = HTML.index("const rowHtmlV2 = (r) =>")
        row = HTML[i:HTML.index("`;", i)]
        self.assertIn('<td class="col-seat">${_teamSeatCellV2(r)}</td>', row)

    def test_seat_cell_does_not_depend_on_2fa(self):
        src = _fn("_teamSeatCellV2")
        self.assertIn("_singleSeatTag(r)", src)
        self.assertIn("_teamFreeTrialTag(r)", src)
        for bad in ("totp_enabled", "totp_secret", "totp_setup_status"):
            self.assertNotIn(bad, src, "Team席位列不该依赖 2FA 字段：%s" % bad)

    def test_totp_cell_no_longer_embeds_seat(self):
        src = _fn("_totpCellV2")
        self.assertNotIn("_singleSeatTag", src, "席位已独立成列，2FA 列里不再重复渲染")


class SeatTagTest(unittest.TestCase):
    """_singleSeatTag 本身不依赖 2FA。"""

    def setUp(self):
        self.src = _fn("_singleSeatTag")

    def test_does_not_read_2fa_fields(self):
        for bad in ("totp_enabled", "totp_secret", "totp_setup_status"):
            self.assertNotIn(bad, self.src, "席位徽章不该依赖 2FA 字段：%s" % bad)

    def test_renders_supported(self):
        self.assertIn("single_seat_supported", self.src)
        self.assertIn("1席", self.src)

    def test_renders_minimum_when_unsupported(self):
        self.assertIn("single_seat_minimum", self.src)

    def test_offers_probe_button_when_unknown(self):
        """没探过的号要给个补测按钮，而不是空白。"""
        self.assertIn("data-probe-single-seat", self.src)
        self.assertIn("测席位", self.src)


class BackendContractTest(unittest.TestCase):
    """后端必须把席位字段下发到列表，否则前端再对也没用。"""

    def test_list_includes_seat_fields(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def _compact_account_for_list")
        seg = src[i:i + 4000]
        self.assertIn("single_seat_supported", seg)
        self.assertIn("single_seat_minimum", seg)

    def test_seat_fields_not_gated_on_2fa(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("single_seat_supported")
        # 往前找 200 字符，确认不是写在什么 2FA 条件里
        seg = src[max(0, i - 200):i]
        self.assertNotIn("totp_enabled", seg)


if __name__ == "__main__":
    unittest.main()
