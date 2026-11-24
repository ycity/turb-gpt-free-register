# -*- coding: utf-8 -*-
"""账号列表的订阅筛选：已订阅 / 未订阅。

线上事故（2026-11-17）：
  「已开通套餐」按钮筛不出任何账号。原因是筛选条件写死 "plus" in plan，
  而这套号实际开的是 Team，库里 current_plan_type 就是 'team'。
  实测分布：free 46 个、team 36 个，plus 命中 0 条。

改动意图：
  - 按钮改名「已订阅」，并把 team/pro/go/business 一并算作已订阅
  - 新增「未订阅」，筛未归档且未订阅的账号
  - 没查过套餐（plan 为空）的不算未订阅，状态未知不该混进来
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


class ButtonTest(unittest.TestCase):
    def test_renamed_to_subscribed(self):
        self.assertIn(">已订阅</button>", HTML)
        self.assertNotIn(">已开通套餐</button>", HTML,
                         "旧名称应已替换")

    def test_unsubscribed_button_exists(self):
        self.assertIn('id="showUnsubscribedAccountsOnlyV2"', HTML)
        self.assertIn(">未订阅</button>", HTML)

    def test_unsubscribed_sits_next_to_subscribed(self):
        """两个按钮应挨着，否则用户要在一排里找。"""
        a = HTML.index('id="showPlusAccountsOnlyV2"')
        b = HTML.index('id="showUnsubscribedAccountsOnlyV2"')
        self.assertLess(a, b)
        self.assertLess(b - a, 400, "两个按钮之间不该隔着别的控件")

    def test_titles_explain_scope(self):
        i = HTML.index('id="showUnsubscribedAccountsOnlyV2"')
        seg = HTML[i:i + 300]
        self.assertIn("未归档", seg, "要说明只看未归档")
        self.assertIn("未订阅", seg)


class FilterValueTest(unittest.TestCase):
    def _fn(self):
        i = HTML.index("function getAccountsPlanFilter()")
        return HTML[i:HTML.index("async function loadAccounts", i)]

    def test_sends_subscribed_not_plus(self):
        """后端认 subscribed；继续发 plus 会漏掉 team。"""
        seg = self._fn()
        self.assertIn("'subscribed'", seg)

    def test_sends_unsubscribed(self):
        self.assertIn("'unsubscribed'", self._fn())

    def test_state_variable_declared(self):
        self.assertIn("let SHOW_UNSUBSCRIBED_ACCOUNTS_ONLY = false;", HTML)


class MutualExclusionTest(unittest.TestCase):
    """四个套餐筛选必须互斥，且加新筛选时不能漏掉某一处同步。"""

    def _table(self):
        i = HTML.index("const ACCOUNTS_PLAN_TOGGLES")
        return HTML[i:HTML.index("async function applyAccountsPlanFilter", i)]

    def test_all_four_toggles_registered(self):
        seg = self._table()
        for el in ("showPlusAccountsOnlyV2", "showUnsubscribedAccountsOnlyV2",
                   "showPlusTrialAccountsOnlyV2", "showFreeAccountsOnlyV2"):
            self.assertIn(el, seg, f"{el} 未登记进互斥表")

    def test_exclusion_is_table_driven(self):
        """收口成一张表，而不是每个函数各自手写一遍。"""
        i = HTML.index("async function applyAccountsPlanFilter")
        seg = HTML[i:HTML.index("async function applyAccountsPlusFilter", i)]
        self.assertIn("for (const t of ACCOUNTS_PLAN_TOGGLES)", seg)

    def test_unsubscribed_has_click_handler(self):
        i = HTML.index("const unsubV2 = $('#showUnsubscribedAccountsOnlyV2')")
        seg = HTML[i:i + 260]
        self.assertIn("applyAccountsUnsubscribedFilter", seg)

    def test_each_filter_has_apply_fn(self):
        for fn in ("applyAccountsPlusFilter", "applyAccountsUnsubscribedFilter",
                   "applyAccountsPlusTrialFilter", "applyAccountsFreeFilter"):
            self.assertIn(f"async function {fn}(on)", HTML)


class BackendContractTest(unittest.TestCase):
    """前端发的值必须是后端认得的。"""

    def test_backend_accepts_what_frontend_sends(self):
        from core import db
        for plan, expect_team in (("subscribed", True), ("unsubscribed", False)):
            row = {"current_plan_type": "team"}
            self.assertEqual(
                db._account_matches_plan_filter(row, plan), expect_team,
                f"后端不认识前端发的 {plan}")

    def test_team_counts_as_subscribed(self):
        """这就是线上筛不出来的那个 case。"""
        from core import db
        self.assertTrue(
            db._account_matches_plan_filter({"current_plan_type": "team"},
                                            "subscribed"))

    def test_unknown_plan_is_neither(self):
        """没查过套餐的账号不该出现在任何一边。"""
        from core import db
        row = {"current_plan_type": ""}
        self.assertFalse(db._account_matches_plan_filter(row, "subscribed"))
        self.assertFalse(db._account_matches_plan_filter(row, "unsubscribed"))


if __name__ == "__main__":
    unittest.main()
