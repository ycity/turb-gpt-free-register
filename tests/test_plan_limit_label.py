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
    """只取 _limitTag 的函数体，不要连带后面的 _teamPlanTag。"""
    start = HTML.index("function _limitTag")
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

    def test_shows_all_windows_not_just_shortest(self):
        """#9 是 5h+weekly，只显示最短的会丢信息。"""
        self.assertIn("join('+')", self.fn)

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


class LimitTagIsIndependentTest(unittest.TestCase):
    """限额是账号级的，不能挂在 workspace 标签上。

    事故：原来限额文本拼在 _teamPlanTag 渲染的 workspace 标签里，而该函数会
    用 excludeAccountId 排除主标签已展示的那个 workspace。personal_plan_type
    为 team 的账号（如 #68）唯一的付费 workspace 正是被排除的那个，于是整个
    标签不渲染，限额跟着消失——页面上完全看不到「周限额」。
    """

    def test_limit_tag_is_a_separate_function(self):
        self.assertIn("function _limitTag", HTML)

    def test_team_tag_no_longer_renders_limit(self):
        start = HTML.index("function _teamPlanTag")
        body = HTML[start:HTML.index("function _planCell")]
        self.assertNotIn("限额", body,
                         "限额不该再由 workspace 标签渲染")

    def test_limit_tag_ignores_workspace_list(self):
        """不读 workspace_plans，就不会被 exclude 规则影响。"""
        fn = _tag_fn()
        self.assertNotIn("workspace_plans", fn)
        self.assertNotIn("excludeAccountId", fn)

    def test_both_branches_render_limit_tag(self):
        start = HTML.index("function _planCell")
        body = HTML[start:HTML.index("function _planAction")]
        self.assertIn("const limitTag = _limitTag(r)", body)
        # free 分支和付费分支各一处
        self.assertEqual(body.count("${limitTag}"), 2,
                         "两个渲染分支都要挂限额标签")

    def test_marks_reached_state(self):
        fn = _tag_fn()
        self.assertIn("plan_limit_reached", fn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
