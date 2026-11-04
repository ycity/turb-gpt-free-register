# -*- coding: utf-8 -*-
"""手动填写并实际用掉的优惠码要登记进池，方便以后查阅。

手动填的码不走 claim_next_promo_code 分派，原先用完就没有记录，翻不到
是谁用了哪个码。
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import team_checkout_service as svc  # noqa: E402


class _Pool:
    """内存版 promo_codes 表，避免碰真实库。"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE promo_codes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " code TEXT UNIQUE, status TEXT, account_id INTEGER,"
            " account_email TEXT, last_error TEXT, created_at TEXT, updated_at TEXT)")

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        self.conn.commit()
        return False

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM promo_codes")]


class RecordUsedPromoTest(unittest.TestCase):
    def setUp(self):
        self.pool = _Pool()
        p = patch.object(svc, "_connect", lambda: self.pool)
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(svc, "get_promo_stats", return_value={})
        p2.start()
        self.addCleanup(p2.stop)

    def test_inserts_new_code_as_used(self):
        r = svc.record_used_promo_code("ABC123", account_id=7,
                                       account_email="a@e.test")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "inserted")
        row = self.pool.rows()[0]
        self.assertEqual(row["status"], "used")
        self.assertEqual(row["account_email"], "a@e.test")

    def test_updates_existing_unused_code(self):
        """池里已有但还没用的码，登记后要标成已用并绑上账号。"""
        self.pool.conn.execute(
            "INSERT INTO promo_codes (code, status) VALUES ('ABC123','unused')")
        r = svc.record_used_promo_code("ABC123", account_id=7,
                                       account_email="a@e.test")
        self.assertEqual(r["action"], "updated")
        self.assertEqual(r["previous_status"], "unused")
        row = self.pool.rows()[0]
        self.assertEqual(row["status"], "used")
        self.assertEqual(row["account_id"], 7)

    def test_does_not_overwrite_existing_owner(self):
        """重复登记同一个码时保留首次归属，别把真实使用者改掉。"""
        self.pool.conn.execute(
            "INSERT INTO promo_codes (code, status, account_email)"
            " VALUES ('ABC123','used','first@e.test')")
        r = svc.record_used_promo_code("ABC123", account_email="second@e.test")
        self.assertEqual(r["action"], "already_used")
        self.assertEqual(self.pool.rows()[0]["account_email"], "first@e.test")

    def test_no_duplicate_rows(self):
        for _ in range(3):
            svc.record_used_promo_code("ABC123", account_email="a@e.test")
        self.assertEqual(len(self.pool.rows()), 1)

    def test_blank_code_rejected(self):
        r = svc.record_used_promo_code("   ")
        self.assertFalse(r["ok"])
        self.assertEqual(self.pool.rows(), [])

    def test_code_is_cleaned(self):
        """手填的码常带前后缀，要走和导入一样的清洗。"""
        with patch.object(svc, "extract_clean_promo_code",
                          return_value="CLEANED") as ex:
            svc.record_used_promo_code("  promo: CLEANED  ")
        ex.assert_called_once()
        self.assertEqual(self.pool.rows()[0]["code"], "CLEANED")


class PaySessionWiringTest(unittest.TestCase):
    def test_recorded_only_after_success(self):
        """只在真正提交成功后登记，填了没付不能算用掉。"""
        src = (Path(__file__).resolve().parents[1]
               / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        done = src.index("state=ST_DONE")
        record = src.index("_record_promo(sess)")
        self.assertLess(done, record)

    def test_record_failure_does_not_break_payment(self):
        from core import vcc_pay_session as ps
        sess = MagicMock(sid="x", account_id=1, email="a@e.test",
                         promo_code="ABC")
        with patch("core.team_checkout_service.record_used_promo_code",
                   side_effect=RuntimeError("db locked")):
            ps._record_promo(sess)

    def test_empty_promo_is_noop(self):
        from core import vcc_pay_session as ps
        sess = MagicMock(sid="x", account_id=1, email="a@e.test", promo_code="")
        with patch("core.team_checkout_service.record_used_promo_code") as rec:
            ps._record_promo(sess)
        rec.assert_not_called()

    def test_app_falls_back_to_manual_code(self):
        """复用已有长链时 meta 为空，要用请求里手填的码兜底。"""
        src = (Path(__file__).resolve().parents[1]
               / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("used_promo =")
        seg = src[i:i + 260]
        self.assertIn('meta.get("promo_used")', seg)
        self.assertIn('data.get("promo_code")', seg)
        self.assertIn("promo_code=used_promo", src)


if __name__ == "__main__":
    unittest.main()
