# -*- coding: utf-8 -*-
"""订阅编号与下次账单列。

编号要反映付款先后：取 max+1 而不是最小未用号。
线上出过问题：手工调整编号留下空位 67，后来 11-13 付款的 #69
填进了这个空位，排到了 11-03 付款的 #45(68) 前面。

下次账单时间用 accounts/check 已有的 renews_at，不额外调接口。
"""
import inspect
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


class AssignOrderTest(unittest.TestCase):
    def test_uses_max_plus_one(self):
        """用最小未用号会让新账号填进旧空位，编号和付款顺序就对不上。"""
        from core import db
        src = inspect.getsource(db.assign_subscription_no)
        self.assertIn("max(used) + 1", src)
        self.assertNotIn("while nxt in used", src)

    def test_reason_documented(self):
        from core import db
        src = inspect.getsource(db.assign_subscription_no)
        self.assertIn("付款先后", src)

    def test_respects_start_floor(self):
        """序列起点不能被击穿，比如 5h 从 49 开始。"""
        from core import db
        src = inspect.getsource(db.assign_subscription_no)
        self.assertIn("if nxt < start", src)

    def test_empty_sequence_uses_start(self):
        from core import db
        src = inspect.getsource(db.assign_subscription_no)
        self.assertIn("if used else start", src)

    def test_existing_number_not_reassigned(self):
        from core import db
        src = inspect.getsource(db.assign_subscription_no)
        self.assertIn("if existing:", src)
        self.assertIn("return int(existing)", src)


class RenewAtColumnTest(unittest.TestCase):
    @staticmethod
    def _table():
        i = HTML.index('id="accountsSelectAllV2"')
        return HTML[HTML.rindex("<colgroup", 0, i):HTML.index("</thead>", i)]

    def test_header_present(self):
        self.assertIn("下次账单", self._table())

    def test_column_counts_match(self):
        """colgroup / thead / 数据行三者列数必须一致，否则表格错位。"""
        head = self._table()
        cols = len(re.findall(r"<col ", head))
        ths = len(re.findall(r"<th ", head))
        i = HTML.index("const rowHtmlV2 = (r) =>")
        body = HTML[i:HTML.index("`;", i)]
        tds = len(re.findall(r"^\s*<td ", body, re.M))
        self.assertEqual(cols, ths, "colgroup 和表头列数不一致")
        self.assertEqual(ths, tds, "表头和数据行列数不一致")

    def test_colspan_updated(self):
        """空状态的 colspan 要跟着列数走。"""
        head = self._table()
        cols = len(re.findall(r"<col ", head))
        i = HTML.index("暂无账号")
        seg = HTML[max(0, i - 200):i]
        m = re.search(r'colspan="(\d+)"', seg)
        self.assertIsNotNone(m, "找不到 colspan")
        self.assertEqual(int(m.group(1)), cols)

    def test_cell_rendered(self):
        """账单时间现在和建号时间合并在 _timeCellV2 里渲染。"""
        self.assertIn("_renewAtValue", HTML)
        i = HTML.index("const rowHtmlV2 = (r) =>")
        self.assertIn("_timeCellV2(r)", HTML[i:HTML.index("`;", i)])

    def test_uses_existing_field_not_new_api(self):
        """renews_at 查套餐时就有了，不该为这一列额外调接口。"""
        i = HTML.index("function _renewAtValue")
        body = HTML[i:i + 1600]
        self.assertIn("plan_renews_at", body)
        self.assertNotIn("fetch(", body)

    def test_warns_before_renewal(self):
        """快到期要能一眼看见，否则会漏掉续费。

        合并成两行后不再用 pill（底色会让密排的两行显得杂乱），
        改成只给时间文字上色。
        """
        i = HTML.index("function _renewAtValue")
        body = HTML[i:i + 1600]
        self.assertIn("is-soon", body)
        self.assertIn("已过期", body)

    def test_warn_class_exists(self):
        """用不存在的 class 不会报错但没样式。"""
        for cls in ("is-soon", "is-expired"):
            self.assertIn(f".acc-v2-time-line.{cls} .acc-v2-time-v", HTML)

    def test_distinguishes_renew_from_expire(self):
        """renews_at 是扣款时刻，expires_at 是权益到期，差几小时。"""
        i = HTML.index("function _renewAtValue")
        body = HTML[i:i + 1600]
        self.assertIn("扣款", body)
        self.assertIn("到期", body)

    def test_free_account_shows_dash(self):
        """没订阅的账号账单行显示 -，不能是空白。"""
        i = HTML.index("function _timeCellV2")
        body = HTML[i:HTML.index("function _discountLabel")]
        self.assertIn("acc-v2-muted", body)


class TimezoneTest(unittest.TestCase):
    """时间统一按新加坡时间（UTC+8）展示。

    后端 created_at 由容器 datetime.now() 产生，容器跑在 UTC 且不带时区标记。
    直接丢给 new Date() 会被按浏览器本地时区解析，差出好几个小时。
    """

    def test_helper_exists(self):
        self.assertIn("function _toSgt", HTML)
        self.assertIn("function _fmtSgt", HTML)

    def test_naive_time_treated_as_utc(self):
        """不带时区的要当成 UTC，不能当本地时间。"""
        i = HTML.index("function _toSgt")
        body = HTML[i:i + 700]
        self.assertIn("hasTz", body)
        self.assertIn("+ 'Z'", body)

    def test_shifts_by_eight_hours(self):
        i = HTML.index("function _fmtSgt")
        body = HTML[i:i + 700]
        self.assertIn("8 * 3600 * 1000", body)

    def test_uses_utc_getters(self):
        """取值必须用 getUTC*，否则又被浏览器时区影响一次。"""
        i = HTML.index("function _fmtSgt")
        body = HTML[i:i + 700]
        for m in ("getUTCFullYear", "getUTCMonth", "getUTCDate",
                  "getUTCHours", "getUTCMinutes"):
            self.assertIn(m, body)
        self.assertNotIn("getFullYear()", body)

    def test_plan_time_delegates(self):
        """_fmtPlanTime 是既有调用点，必须走同一套转换。"""
        i = HTML.index("function _fmtPlanTime")
        self.assertIn("_fmtSgt", HTML[i:i + 200])

    def test_created_at_uses_sgt(self):
        """建号时间现在在 _timeCellV2 里渲染，时区转换不能丢。"""
        i = HTML.index("function _timeCellV2")
        body = HTML[i:HTML.index("function _discountLabel")]
        self.assertIn("_fmtSgt(r.created_at)", body)

    def test_headers_mention_timezone(self):
        i = HTML.index('id="accountsSelectAllV2"')
        head = HTML[HTML.rindex("<colgroup", 0, i):HTML.index("</thead>", i)]
        self.assertIn("UTC+8", head)


if __name__ == "__main__":
    unittest.main(verbosity=2)
