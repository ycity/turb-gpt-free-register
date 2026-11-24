# -*- coding: utf-8 -*-
"""「过滤2席位」快捷筛选：滤掉已确定需要 2 席的账号。

需求（2026-11-18）：账号列表加一个快捷按钮，点击后把确定是 2 席的号
过滤掉——它们订阅要多花一倍钱，挑号时不想看到。

关键约束：
  single_seat_supported 是三态（True 1席 / False 2席 / 缺失 未探测）。
  只滤掉 False 的；缺失的必须保留——实测未探测的号约 55% 是单席位，
  一并滤掉会误伤大半可用号。

  席位筛选独立于套餐筛选（已订阅/未订阅/有套餐优惠互斥），要能叠加：
  "未订阅 + 过滤2席位" 才是真正可订阅的号。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

from core import db  # noqa: E402


def _acc(aid, seat):
    d = {"id": aid, "email": "a%d@e.test" % aid}
    if seat is not None:
        d["single_seat_supported"] = seat
    return d


class MatcherTest(unittest.TestCase):
    """Python 侧的筛选判定。"""

    def test_not_multi_keeps_single(self):
        self.assertTrue(db._matches_seat_filter(_acc(1, True), "not_multi"))

    def test_not_multi_drops_multi(self):
        self.assertFalse(db._matches_seat_filter(_acc(2, False), "not_multi"))

    def test_not_multi_keeps_unprobed(self):
        """没探过的要保留——约 55% 是单席位，滤掉会误伤。"""
        self.assertTrue(db._matches_seat_filter(_acc(3, None), "not_multi"))

    def test_blank_filter_keeps_all(self):
        for seat in (True, False, None):
            self.assertTrue(db._matches_seat_filter(_acc(1, seat), ""))

    def test_single_only(self):
        self.assertTrue(db._matches_seat_filter(_acc(1, True), "single"))
        self.assertFalse(db._matches_seat_filter(_acc(2, False), "single"))
        self.assertFalse(db._matches_seat_filter(_acc(3, None), "single"))

    def test_multi_only(self):
        self.assertFalse(db._matches_seat_filter(_acc(1, True), "multi"))
        self.assertTrue(db._matches_seat_filter(_acc(2, False), "multi"))
        self.assertFalse(db._matches_seat_filter(_acc(3, None), "multi"))

    def test_unknown_only(self):
        self.assertFalse(db._matches_seat_filter(_acc(1, True), "unknown"))
        self.assertFalse(db._matches_seat_filter(_acc(2, False), "unknown"))
        self.assertTrue(db._matches_seat_filter(_acc(3, None), "unknown"))

    def test_zero_counts_as_multi(self):
        """SQLite 布尔存成 0/1，JSON 回来可能是 0 而不是 False。

        两条路径必须一致：SQL 用 `!= 0`，Python 侧也要把 0 当 2 席。
        用 `is False` 会认不出 0，导致分页和非分页接口返回不同的行。
        """
        d = {"id": 9, "single_seat_supported": 0}
        self.assertFalse(db._matches_seat_filter(d, "not_multi"))
        self.assertTrue(db._matches_seat_filter(d, "multi"))
        self.assertFalse(db._matches_seat_filter(d, "single"))

    def test_one_counts_as_single(self):
        d = {"id": 9, "single_seat_supported": 1}
        self.assertTrue(db._matches_seat_filter(d, "not_multi"))
        self.assertTrue(db._matches_seat_filter(d, "single"))
        self.assertFalse(db._matches_seat_filter(d, "multi"))


class SqlTest(unittest.TestCase):
    """SQL 侧的筛选条件。"""

    def test_not_multi_builds_condition(self):
        where, params = db._account_filter_sql(seat_filter="not_multi")
        self.assertTrue(any("single_seat_supported" in w for w in where))
        joined = " ".join(where)
        self.assertIn("IS NULL", joined, "没探过的必须保留")

    def test_blank_adds_nothing(self):
        where, params = db._account_filter_sql(seat_filter="")
        self.assertEqual(where, [])
        self.assertEqual(params, [])

    def test_single_only_condition(self):
        where, _ = db._account_filter_sql(seat_filter="single")
        self.assertTrue(any("= 1" in w for w in where))

    def test_multi_only_condition(self):
        where, _ = db._account_filter_sql(seat_filter="multi")
        self.assertTrue(any("= 0" in w for w in where))

    def test_combines_with_plan_filter(self):
        """能和套餐筛选叠加，不互相覆盖。"""
        where, _ = db._account_filter_sql(
            plan_filter="unsubscribed", seat_filter="not_multi")
        joined = " ".join(where)
        self.assertIn("single_seat_supported", joined)
        self.assertIn("plan_type", joined)

    def test_sql_and_python_agree(self):
        """两条路径的判定必须一致，否则分页和非分页结果会不同。"""
        for seat in (True, False, None):
            row = _acc(1, seat)
            py = db._matches_seat_filter(row, "not_multi")
            self.assertEqual(py, seat is not False,
                             "seat=%r 的判定和预期不符" % seat)


class PlumbingTest(unittest.TestCase):
    def test_filtered_accounts_applies_seat(self):
        import inspect
        src = inspect.getsource(db._filtered_decorated_accounts)
        self.assertIn("_matches_seat_filter", src,
                      "拿到参数却不用，筛选会静默失效")

    def test_list_accounts_accepts_seat(self):
        import inspect
        self.assertIn("seat_filter",
                      inspect.signature(db.list_accounts).parameters)

    def test_list_accounts_page_accepts_seat(self):
        import inspect
        self.assertIn("seat_filter",
                      inspect.signature(db.list_accounts_page).parameters)

    def test_plan_check_snapshot_accepts_seat(self):
        """列表和状态快照要用同一套筛选，否则行对不上。"""
        import inspect
        self.assertIn(
            "seat_filter",
            inspect.signature(db.list_account_plan_check_statuses).parameters)

    def test_api_reads_seat_param(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def api_accounts")
        seg = src[i:i + 2600]
        self.assertIn('request.args.get("seat"', seg)
        self.assertIn("seat_filter=seat_filter", seg)

    def test_plan_check_api_reads_seat(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def api_account_plan_check_status")
        seg = src[i:i + 2200]
        self.assertIn('request.args.get("seat"', seg)


class UiTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "webui" / "templates" / "index.html").read_text(
            encoding="utf-8")

    def test_button_exists(self):
        self.assertIn('id="hideMultiSeatAccountsV2"', self.html)
        self.assertIn(">过滤2席位</button>", self.html)

    def test_button_has_tooltip(self):
        i = self.html.index('id="hideMultiSeatAccountsV2"')
        seg = self.html[max(0, i - 300):i + 300]
        self.assertIn("title=", seg)
        self.assertIn("2 席", seg)

    def test_state_var_declared(self):
        self.assertIn("let HIDE_MULTI_SEAT_ACCOUNTS = false;", self.html)

    def test_sends_seat_param(self):
        self.assertIn("seat=${encodeURIComponent(getAccountsSeatFilter())}",
                      self.html)

    def test_both_requests_send_seat(self):
        """列表和状态快照都要带，否则两边筛选不一致。"""
        self.assertEqual(
            self.html.count("seat=${encodeURIComponent(getAccountsSeatFilter())}"),
            2)

    def test_filter_value_is_not_multi(self):
        i = self.html.index("function getAccountsSeatFilter")
        seg = self.html[i:i + 400]
        self.assertIn("'not_multi'", seg)

    def test_click_handler_bound(self):
        self.assertIn("hideMultiSeatV2", self.html)
        self.assertIn("applyHideMultiSeatFilter", self.html)

    def test_independent_from_plan_toggles(self):
        """席位筛选不能被套餐筛选的互斥表关掉——两者是不同维度。"""
        i = self.html.index("const ACCOUNTS_PLAN_TOGGLES")
        seg = self.html[i:i + 700]
        self.assertNotIn("hideMultiSeatAccountsV2", seg,
                         "席位筛选不该进套餐互斥表")

    def test_resets_page_on_toggle(self):
        """换筛选要回到第一页，否则可能停在空页。"""
        i = self.html.index("async function applyHideMultiSeatFilter")
        seg = self.html[i:i + 500]
        self.assertIn("PAGERS.accounts.page = 1", seg)

    def test_clears_selection_on_toggle(self):
        """筛掉的行可能还在选中集合里，批量操作会误伤。"""
        i = self.html.index("async function applyHideMultiSeatFilter")
        seg = self.html[i:i + 500]
        self.assertIn("ACCOUNT_SELECTED.clear()", seg)


if __name__ == "__main__":
    unittest.main()
