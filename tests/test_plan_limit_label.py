# -*- coding: utf-8 -*-
"""账号列表的限额标签要用套餐限额，不要用聊天额度。

旧实现标签读 limit_windows（conversation/init 的聊天额度，按剩余时间推断），
于是 #68 这个 weekly 账号被标成「日限额」，和 settings/usage 页显示的
「每周限额」对不上。应该优先用 plan_limit_windows。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")


def _tag_fn():
    """只取 _limitSuffix 的函数体，不要连带后面的 _teamPlanTag。"""
    start = HTML.index("function _limitSuffix")
    return HTML[start:HTML.index("function _teamPlanTag", start)]


class ApiExposesFieldsTest(unittest.TestCase):
    def test_list_api_sends_plan_limit(self):
        for f in ("plan_limit_windows", "plan_limit_detail"):
            self.assertIn(f'"{f}"', APP, f"列表接口没下发 {f}")

    def test_sent_alongside_existing_limit_fields(self):
        idx = APP.index('"plan_limit_windows"')
        window = APP[max(0, idx - 400):idx]
        self.assertIn("limit_windows", window)


class TagPrefersPlanLimitTest(unittest.TestCase):
    def setUp(self):
        self.fn = _tag_fn()

    def test_reads_plan_limit_windows(self):
        self.assertIn("plan_limit_windows", self.fn)

    def test_plan_limit_takes_priority(self):
        """有套餐限额时用它，没有才回落到聊天额度。"""
        self.assertIn("planWins.length ? planWins : chatWins", self.fn)

    def test_keeps_chat_limit_as_fallback(self):
        self.assertIn("limit_windows", self.fn)

    def test_shows_only_shortest_window(self):
        """只标最短窗口。5h+周 这种拼接读起来啰嗦，完整窗口放 title。"""
        fn = _tag_fn()
        self.assertIn("shortest", fn)
        self.assertNotIn("join('+') + '限额'", fn)

    def test_full_windows_kept_in_tooltip(self):
        fn = _tag_fn()
        self.assertIn("const all = windows.map", fn)

    def test_tooltip_separates_two_data_sources(self):
        """两套额度含义不同，提示里不能混为一谈。"""
        self.assertIn("套餐限额", self.fn)
        self.assertIn("聊天额度", self.fn)

    def test_tooltip_shows_used_percent(self):
        self.assertIn("used_percent", self.fn)

    def test_comment_explains_why(self):
        self.assertIn("推断", self.fn)


class LabelMappingTest(unittest.TestCase):
    def test_weekly_renders_as_week(self):
        m = re.search(r"const LIMIT_WINDOW_LABEL = \{([^}]*)\}", HTML)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("weekly: '周'", body)
        self.assertIn("'5h': '5h'", body)


class LimitFollowsWorkspaceTest(unittest.TestCase):
    """限额跟随空间，不是账号级的。

    查询时按 paid_workspace_account_id 指定 workspace 发起，拿到的就是那个
    空间的额度，所以要和付费空间的套餐显示在一起：
      personal=free 的账号 → 挂在 TEAM 这个 workspace 标签上
      personal=team 的账号 → 付费空间就是主标签那个，并进主标签
    曾经拆成独立标签，但那样和数据归属对不上，反直觉。
    """

    def test_limit_is_a_suffix_not_a_standalone_tag(self):
        self.assertIn("function _limitSuffix", HTML)
        self.assertNotIn("function _limitTag", HTML)

    def test_returns_text_and_tip(self):
        fn = _tag_fn()
        self.assertIn("return {text, tip}", fn)

    def test_workspace_tag_appends_limit(self):
        start = HTML.index("function _teamPlanTag")
        body = HTML[start:HTML.index("function _planCell")]
        self.assertIn("_limitSuffix(r)", body)
        self.assertIn("${suffix}", body)

    def test_only_the_queried_workspace_shows_limit(self):
        """账号有多个付费空间时，别的空间不能蹭这个限额。"""
        start = HTML.index("function _teamPlanTag")
        body = HTML[start:HTML.index("function _planCell")]
        self.assertIn("paid_workspace_account_id", body)
        self.assertIn("const mine =", body)

    def test_paid_branch_merges_limit_into_main_label(self):
        start = HTML.index("function _planCell")
        body = HTML[start:HTML.index("function _planAction")]
        self.assertIn("const limit = _limitSuffix(r)", body)

    def test_limit_is_not_inside_billing_parens(self):
        """括号里是计费信息（月付/EGP/到期），限额不该混进去——
        那样 personal=team 的账号写法和 personal=free 的不一致。"""
        start = HTML.index("function _planCell")
        body = HTML[start:HTML.index("function _planAction")]
        self.assertNotIn("parts.push(limit.text)", body)
        self.assertIn("${limit.text}", body)
        self.assertIn("limit.text ?", body)

    def test_free_branch_does_not_duplicate(self):
        """free 分支的限额已经在 workspace 标签里，不能再加一次。"""
        start = HTML.index("function _planCell")
        body = HTML[start:HTML.index("function _planAction")]
        free_part = body[body.index("if (baseLower === 'free')"):
                         body.index("const cls = 'status-success'")]
        self.assertNotIn("_limitSuffix", free_part)

    def test_marks_reached_state(self):
        fn = _tag_fn()
        self.assertIn("plan_limit_reached", fn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
