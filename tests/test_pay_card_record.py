# -*- coding: utf-8 -*-
"""记录订阅是用哪张卡开通的。

问题（2026-11-18 发现）：
  37 个已付款账号里，0 个记了卡信息。只有一个全局 pay_last_card_id，
  每付一单就被覆盖，事后根本查不到某个账号是哪张卡付的——
  续费扣款、对账、某张卡被风控时想知道牵连哪些号，都要用这个。

记录规则（按用户指定）：
  VCC 托管卡 -> VCC-{卡号后四位}
  自有卡池卡 -> 卡池里填的备注名（label）
历史数据不补：只有 pay_last_card_id 一个线索且已被反复覆盖，硬补会记错。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class SessionFieldTest(unittest.TestCase):
    def test_session_has_card_label(self):
        from core.vcc_pay_session import PaySession
        s = PaySession(sid="x", account_id=1)
        self.assertEqual(s.card_label, "")

    def test_public_view_exposes_label(self):
        from core.vcc_pay_session import PaySession
        s = PaySession(sid="x", account_id=1)
        s.card_label = "VCC-1771"
        self.assertEqual(s.public().get("card_label"), "VCC-1771")

    def test_public_view_has_no_raw_card_number(self):
        """标签不能把卡号明文带出去。"""
        from core.vcc_pay_session import PaySession
        s = PaySession(sid="x", account_id=1)
        s.card_label = "VCC-1771"
        s.card_mask = "537872713****1771"
        blob = str(s.public())
        self.assertNotIn("5378727131", blob)


class LabelRuleTest(unittest.TestCase):
    """两种卡的标签规则。"""

    def _src(self):
        import inspect
        from core import vcc_pay_session as ps
        return inspect.getsource(ps._prepare_payment) if hasattr(
            ps, "_prepare_payment") else (
            (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8"))

    def test_vcc_card_uses_vcc_prefix(self):
        src = self._src()
        self.assertIn('"VCC-%s" % tail', src)

    def test_custom_card_prefers_pool_label(self):
        src = self._src()
        self.assertIn('sec.get("label")', src)

    def test_custom_card_falls_back_to_vcc_prefix(self):
        """自有卡没填备注名时也要有个可读标识，不能留空。"""
        src = self._src()
        i = src.index('sec.get("label")')
        seg = src[i:i + 400]
        self.assertIn("VCC-", seg)

    def test_secrets_returns_label(self):
        """get_secrets 要带上备注名，省得支付时再查一次卡池。"""
        import inspect
        from core import custom_card_store
        src = inspect.getsource(custom_card_store.get_secrets)
        self.assertIn('"label"', src)


class PersistTest(unittest.TestCase):
    def test_db_function_exists(self):
        from core import db
        self.assertTrue(hasattr(db, "set_account_pay_card"))

    def test_writes_label(self):
        from core import db
        rows = [{"id": 7}]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts") as save:
            ok = db.set_account_pay_card(7, "VCC-1771")
        self.assertTrue(ok)
        self.assertEqual(rows[0]["pay_card_label"], "VCC-1771")
        save.assert_called_once()

    def test_does_not_overwrite_existing(self):
        """首次支付用哪张卡才是要查的，重复调用不能冲掉。"""
        from core import db
        rows = [{"id": 7, "pay_card_label": "VCC-1111"}]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts") as save:
            ok = db.set_account_pay_card(7, "VCC-9999")
        self.assertFalse(ok)
        self.assertEqual(rows[0]["pay_card_label"], "VCC-1111")
        save.assert_not_called()

    def test_blank_label_rejected(self):
        from core import db
        with patch.object(db, "_load_accounts", return_value=[{"id": 7}]), \
             patch.object(db, "_save_accounts") as save:
            self.assertFalse(db.set_account_pay_card(7, "   "))
        save.assert_not_called()

    def test_missing_account_is_safe(self):
        from core import db
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_save_accounts") as save:
            self.assertFalse(db.set_account_pay_card(99, "VCC-1"))
        save.assert_not_called()

    def test_label_length_capped(self):
        """备注名可能很长，截断避免撑爆账号表。"""
        from core import db
        rows = [{"id": 7}]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts"):
            db.set_account_pay_card(7, "x" * 200)
        self.assertLessEqual(len(rows[0]["pay_card_label"]), 64)


class WiringTest(unittest.TestCase):
    def test_recorded_on_payment_success(self):
        import inspect
        from core import vcc_pay_session as ps
        src = inspect.getsource(ps._record_paid_at)
        self.assertIn("set_account_pay_card", src)

    def test_record_failure_does_not_break_payment(self):
        """钱已经付了，记录失败不能把结果搞成失败。"""
        import inspect
        from core import vcc_pay_session as ps
        src = inspect.getsource(ps._record_paid_at)
        i = src.index("set_account_pay_card")
        self.assertIn("except", src[i:], "写库失败要吞掉")

    def test_only_after_success(self):
        """只在支付真正成功后记，填了没付不能算。"""
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        done = src.index("state=ST_DONE")
        rec = src.index("_record_paid_at(sess)")
        self.assertLess(done, rec)


class DisplayTest(unittest.TestCase):
    def test_backend_sends_field(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def _compact_account_for_list")
        self.assertIn("pay_card_label", src[i:i + 4000])


class CardColumnTest(unittest.TestCase):
    """银行卡是独立一列，不挤在时间列里。"""

    def setUp(self):
        self.html = (ROOT / "webui" / "templates" / "index.html").read_text(
            encoding="utf-8")

    def _table_head(self):
        i = self.html.index('id="accountsSelectAllV2"')
        return self.html[self.html.rindex("<colgroup", 0, i):
                         self.html.index("</thead>", i)]

    def test_header_exists(self):
        self.assertIn(">银行卡</th>", self._table_head())

    def test_colgroup_has_card_col(self):
        self.assertIn('col class="col-card"', self._table_head())

    def test_column_counts_match(self):
        """colgroup 的 col 数必须和 th 数一致，否则表头会错位。"""
        head = self._table_head()
        cols = head.count("<col ")
        ths = head.count("<th ")
        self.assertEqual(cols, ths, "col 和 th 数量不一致，表格会错位")

    def test_row_renders_card_cell(self):
        i = self.html.index("const rowHtmlV2 = (r) =>")
        seg = self.html[i:self.html.index("`;", i)]
        self.assertIn('<td class="col-card">', seg)
        self.assertIn("_payCardCellV2(r)", seg)

    def test_row_cell_count_matches_header(self):
        """每行的 td 数要和表头一致。"""
        head = self._table_head()
        i = self.html.index("const rowHtmlV2 = (r) =>")
        row = self.html[i:self.html.index("`;", i)]
        self.assertEqual(row.count("<td "), head.count("<th "),
                         "td 和 th 数量不一致，表格会错位")

    def test_empty_state_colspan_updated(self):
        """加列后空状态的 colspan 要跟着改，否则占不满。"""
        head = self._table_head()
        n = head.count("<th ")
        self.assertIn('colspan="%d"' % n, self.html)

    def test_column_has_width(self):
        self.assertIn(".accounts-table-v2 .col-card { width:", self.html)

    def test_time_cell_no_longer_has_card(self):
        """卡片已独立成列，时间列里不该再有。"""
        i = self.html.index("function _timeCellV2(")
        seg = self.html[i:i + 1500]
        self.assertNotIn("pay_card_label", seg)

    def test_cell_renders_label(self):
        i = self.html.index("function _payCardCellV2(")
        seg = self.html[i:i + 1200]
        self.assertIn("pay_card_label", seg)

    def test_cell_distinguishes_paid_without_card(self):
        """上线前付过款的老账号要能看出是"没记录"，不是"没订阅"。"""
        i = self.html.index("function _payCardCellV2(")
        seg = self.html[i:i + 1200]
        self.assertIn("paid_at", seg)
        self.assertIn("上线前", seg)

    def test_cell_does_not_leak_card_number(self):
        i = self.html.index("function _payCardCellV2(")
        seg = self.html[i:i + 1200]
        self.assertNotIn("card_mask", seg)
        self.assertNotIn("card_no", seg)


if __name__ == "__main__":
    unittest.main()
