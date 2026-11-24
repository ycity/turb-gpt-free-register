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


class ExplicitPromoRegistrationTest(unittest.TestCase):
    """显式传码提链也要登记进池。

    2026-11-17 线上事故（账号 #83）：指定 TXSFY6YAAG8YMS4J 提链，码被
    OpenAI 真实消耗、账单打了八折（EGP 920 = 1150×0.8），但池里那条
    一直挂着 unused。原因是显式传码走 `if promo` 分支，assigned 恒为
    None，成功分支里那句 mark_promo_code 根本不执行。
    后果：下次提链会把这个废码再分派一次，白白失败一轮。
    """

    def _call(self, *, url, promo, err="", account_id=83,
              email="lazusky@e.test"):
        """跑一遍 generate_team_checkout_link 的尾段，返回 record 调用。"""
        with patch.object(svc, "_native_checkout",
                          return_value=(url, {"single_seat": True} if url else None, err)), \
             patch.object(svc, "claim_next_promo_code") as claim, \
             patch.object(svc, "mark_promo_code") as mark, \
             patch.object(svc, "record_used_promo_code",
                          return_value={"ok": True}) as rec, \
             patch.object(svc, "_maybe_auto_pay", return_value=None), \
             patch.object(svc, "token_claims_ok", create=True):
            res = svc.generate_team_checkout_link(
                token="tok", proxy_address="http://127.0.0.1:1",
                promo_code=promo, use_1024proxy=False,
                account_id=account_id, account_email=email)
        return res, claim, mark, rec

    def test_explicit_code_recorded_on_success(self):
        """这就是 #83 的场景：显式传码 + 出链成功 → 必须登记。"""
        res, claim, mark, rec = self._call(url="https://x/oaics_1",
                                           promo="TXSFY6YAAG8YMS4J")
        self.assertTrue(res["ok"])
        claim.assert_not_called()      # 显式传码不该从池里领
        mark.assert_not_called()       # assigned 为 None，走不到 mark
        rec.assert_called_once()
        self.assertEqual(rec.call_args.args[0], "TXSFY6YAAG8YMS4J")
        self.assertEqual(rec.call_args.kwargs["account_id"], 83)

    def test_pool_assigned_code_still_uses_mark(self):
        """没传码时仍走池子分派那条老路，不能被新逻辑顶掉。"""
        with patch.object(svc, "_native_checkout",
                          return_value=("https://x/1", {}, "")), \
             patch.object(svc, "claim_next_promo_code",
                          return_value={"id": 7, "code": "POOLCODE"}), \
             patch.object(svc, "mark_promo_code") as mark, \
             patch.object(svc, "record_used_promo_code") as rec, \
             patch.object(svc, "_maybe_auto_pay", return_value=None):
            res = svc.generate_team_checkout_link(
                token="tok", proxy_address="http://127.0.0.1:1",
                promo_code="", use_1024proxy=False,
                account_id=5, account_email="a@e.test")
        self.assertTrue(res["ok"])
        mark.assert_called_once()
        self.assertEqual(mark.call_args.args[0], 7)
        rec.assert_not_called()        # 池子分派用 mark，不重复登记

    def test_explicit_code_marked_expired_when_not_eligible(self):
        """显式传的码被判不可用时要落成 expired，否则下次还会被挑出来。"""
        res, claim, mark, rec = self._call(
            url=None, promo="DEADCODE",
            err="Discount code is not eligible [HTTP 400]")
        self.assertFalse(res["ok"])
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["status"], "expired")

    def test_explicit_code_untouched_on_network_failure(self):
        """代理超时之类与码无关，不能冤枉好码。"""
        res, claim, mark, rec = self._call(
            url=None, promo="GOODCODE",
            err="Timeout: curl: (28) Connection timed out")
        self.assertFalse(res["ok"])
        rec.assert_not_called()

    def test_no_promo_no_registration(self):
        """既没传码、池子也没码时，不该凭空登记一条。"""
        with patch.object(svc, "_native_checkout",
                          return_value=("https://x/1", {}, "")), \
             patch.object(svc, "claim_next_promo_code", return_value=None), \
             patch.object(svc, "mark_promo_code") as mark, \
             patch.object(svc, "record_used_promo_code") as rec, \
             patch.object(svc, "_maybe_auto_pay", return_value=None):
            svc.generate_team_checkout_link(
                token="tok", proxy_address="http://127.0.0.1:1",
                promo_code="", use_1024proxy=False, account_id=5)
        mark.assert_not_called()
        rec.assert_not_called()

    def test_registration_failure_does_not_break_link(self):
        """登记失败不能吃掉已经拿到的长链——链比记录值钱。"""
        with patch.object(svc, "_native_checkout",
                          return_value=("https://x/1", {}, "")), \
             patch.object(svc, "claim_next_promo_code", return_value=None), \
             patch.object(svc, "record_used_promo_code",
                          side_effect=RuntimeError("db locked")), \
             patch.object(svc, "_maybe_auto_pay", return_value=None):
            res = svc.generate_team_checkout_link(
                token="tok", proxy_address="http://127.0.0.1:1",
                promo_code="SOMECODE", use_1024proxy=False, account_id=5)
        self.assertTrue(res["ok"])
        self.assertEqual(res["url"], "https://x/1")


class AutoPayOptOutTest(unittest.TestCase):
    """2026-11-18 事故：同一个长链被付了两次。

    generate_team_checkout_link 内部会调 _maybe_auto_pay（裸 pay_checkout，
    不带登录 cookie），单席位结账页打开只会落到 /auth/login；而调用方随后
    还会用 vcc_pay_session 再付一次（那条会先登录注入 session-token）。
    前者注定失败却已经把链打开一遍，等于白烧。auto_pay=False 关掉前者。
    """

    def _call(self, **kw):
        with patch.object(svc, "_native_checkout",
                          return_value=("https://x/oaics_1", {}, "")), \
             patch.object(svc, "claim_next_promo_code", return_value=None), \
             patch.object(svc, "_maybe_auto_pay",
                          return_value={"ok": True}) as auto_pay:
            res = svc.generate_team_checkout_link(
                token="tok", proxy_address="http://127.0.0.1:1",
                use_1024proxy=False, account_id=5,
                account_email="a@e.test", **kw)
        return res, auto_pay

    def test_auto_pay_false_skips_payment_attempt(self):
        res, auto_pay = self._call(auto_pay=False)
        self.assertTrue(res["ok"])
        auto_pay.assert_not_called()
        self.assertNotIn("auto_pay", res)

    def test_auto_pay_defaults_to_true_for_manual_flow(self):
        """手动提链仍要自动付一次，别把默认行为改掉。"""
        res, auto_pay = self._call()
        auto_pay.assert_called_once()
        self.assertEqual(res["auto_pay"], {"ok": True})

    def test_auto_subscribe_passes_auto_pay_false(self):
        """自动订阅必须关掉提链内置支付，否则和第 2 次支付撞车。"""
        import inspect
        from core import auto_subscribe_scheduler as sched

        src = inspect.getsource(sched.start_subscription)
        self.assertIn("auto_pay=False", src)

    def test_manual_webui_checkout_passes_auto_pay_false(self):
        """WebUI 提链接口后面也会起 pay-session，同样不能先付一次。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        src = (root / "webui" / "app.py").read_text(encoding="utf-8")
        # /api/accounts/pay/start：生成长链 + 随后 start_session 支付
        i = src.index("def api_account_pay_start")
        seg = src[i:i + 9000]
        self.assertIn("auto_pay=False", seg)


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
