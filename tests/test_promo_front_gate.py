# -*- coding: utf-8 -*-
"""优惠码前置闸门与失败退码。

问题（2026-11-20 补跑事故）：
  池里最后一个码被失败的候选领走后一直挂在 using
  （reclaim_stale_promo_codes 写了但从未被调度），后面的候选领不到码，
  仍按原价 EGP 1150 跑完选卡、充值、验出口、提链、登录、填卡的全流程，
  最后才被金额闸门拦下（上限 920 = 八折价），3 连败烧完整轮。

修法：
  - start_subscription 开头先领码（前置闸门），没码立即失败，不烧流程；
  - 领码后的每个失败路径把未消耗的码退回 unused（release_promo_code）；
  - run_once 每轮先回收陈旧 using 码，池子空了整轮直接跳过。
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
            " account_email TEXT, last_error TEXT, created_at TEXT,"
            " updated_at TEXT)")

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        self.conn.commit()
        return False

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM promo_codes")]


def _seed_code(pool, code="GATECODE123", status="using", account_id=162):
    with pool as conn:
        conn.execute(
            "INSERT INTO promo_codes (code, status, account_id,"
            " account_email, created_at, updated_at)"
            " VALUES (?, ?, ?, 'a@e.test', 'now', 'now')",
            (code, status, account_id))


class ReleasePromoCodeTest(unittest.TestCase):
    """release_promo_code：只退 using，不碰 used。"""

    def setUp(self):
        self.pool = _Pool()
        p = patch.object(svc, "_connect", lambda: self.pool)
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(svc, "get_promo_stats", return_value={})
        p2.start()
        self.addCleanup(p2.stop)

    def test_releases_using_code(self):
        _seed_code(self.pool)
        self.assertTrue(svc.release_promo_code("GATECODE123", "支付失败"))
        row = self.pool.rows()[0]
        self.assertEqual(row["status"], "unused")
        self.assertIsNone(row["account_id"])
        self.assertIsNone(row["account_email"])
        self.assertIn("支付失败", row["last_error"])

    def test_never_touches_used_code(self):
        """支付实际提交成功过的码（used）绝不能退回，否则二次分派废码。"""
        _seed_code(self.pool, status="used")
        self.assertFalse(svc.release_promo_code("GATECODE123"))
        self.assertEqual(self.pool.rows()[0]["status"], "used")

    def test_never_touches_unused_code(self):
        _seed_code(self.pool, status="unused", account_id=None)
        self.assertFalse(svc.release_promo_code("GATECODE123"))
        self.assertEqual(self.pool.rows()[0]["status"], "unused")

    def test_idempotent(self):
        _seed_code(self.pool)
        self.assertTrue(svc.release_promo_code("GATECODE123"))
        self.assertFalse(svc.release_promo_code("GATECODE123"))

    def test_blank_or_garbage_rejected(self):
        self.assertFalse(svc.release_promo_code(""))
        self.assertFalse(svc.release_promo_code(None))


class GenerateWithAssignedPromoTest(unittest.TestCase):
    """promo_assigned 走 assigned 通道：成功留 using，提链失败退回。"""

    def setUp(self):
        self.pool = _Pool()
        p = patch.object(svc, "_connect", lambda: self.pool)
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(svc, "get_promo_stats", return_value={})
        p2.start()
        self.addCleanup(p2.stop)
        _seed_code(self.pool, status="unused", account_id=None)
        self.assigned = self.pool.rows()[0]

    def _run(self, native_side_effect):
        with patch.object(svc, "_native_checkout", side_effect=native_side_effect), \
             patch.object(svc, "_maybe_auto_pay", return_value=None):
            return svc.generate_team_checkout_link(
                token="tok", proxy_address="http://p:1", country="EG",
                currency="EGP", promo_assigned=self.assigned,
                account_id=162, account_email="a@e.test", auto_pay=False)

    def test_success_keeps_using(self):
        r = self._run(lambda *a, **k: ("https://c/oaics_x", {}, ""))
        self.assertTrue(r["ok"])
        self.assertEqual(r["promo_used"], "GATECODE123")
        self.assertTrue(r["promo_from_pool"])
        row = self.pool.rows()[0]
        self.assertEqual(row["status"], "using", "提链成功只能留 using")
        self.assertEqual(row["account_id"], 162)

    def test_link_failure_releases(self):
        r = self._run(lambda *a, **k: (None, None, "Timeout: curl: (28)"))
        self.assertFalse(r["ok"])
        row = self.pool.rows()[0]
        self.assertEqual(row["status"], "unused", "提链失败要把码退回池子")


class StartSubscriptionFrontGateTest(unittest.TestCase):
    """start_subscription：领不到码立即失败，不碰卡、不出代理流量。"""

    def test_no_code_fails_fast(self):
        from core import auto_subscribe_scheduler as sched
        from core import db as db_mod
        from core import vcc_client

        client = MagicMock()
        with patch.object(db_mod, "get_account",
                          return_value={"id": 162, "email": "a@e.test",
                                        "access_token": "tok"}), \
             patch("core.team_checkout_service.claim_next_promo_code",
                   return_value=None), \
             patch.object(vcc_client, "build_client_from_config",
                          return_value=client) as build:
            res = sched.start_subscription(162)
        self.assertFalse(res["ok"])
        self.assertIn("优惠码池没有可用码（前置闸门）", res["error"])
        build.assert_not_called()
        client.list_cards.assert_not_called()


class RunOnceRoundGateTest(unittest.TestCase):
    """run_once：池子空了整轮跳过，且先回收陈旧 using 码。"""

    def test_empty_pool_skips_round(self):
        from core import auto_subscribe_scheduler as sched

        acc = {"id": 162, "email": "a@e.test", "access_token": "tok"}
        with patch.object(sched, "is_enabled", return_value=True), \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates", return_value=[acc]), \
             patch("core.team_checkout_service.reclaim_stale_promo_codes",
                   return_value={"freed": ["OLD1"], "count": 1}) as reclaim, \
             patch("core.team_checkout_service.get_promo_stats",
                   return_value={"total": 1, "unused": 0, "using": 1,
                                 "used": 0, "expired": 0}):
            res = sched.run_once(force=True)
        self.assertFalse(res["ok"])
        self.assertIn("优惠码池没有可用码（轮级前置闸门）", res["reason"])
        reclaim.assert_called_once()
        self.assertEqual(res["tried"], [], "池空时不应尝试任何候选")

    def test_pool_with_codes_proceeds(self):
        from core import auto_subscribe_scheduler as sched

        acc = {"id": 162, "email": "a@e.test", "access_token": "tok"}
        with patch.object(sched, "is_enabled", return_value=True), \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates", return_value=[acc]), \
             patch("core.team_checkout_service.reclaim_stale_promo_codes",
                   return_value={"freed": [], "count": 0}), \
             patch("core.team_checkout_service.get_promo_stats",
                   return_value={"total": 1, "unused": 1, "using": 0,
                                 "used": 0, "expired": 0}), \
             patch.object(sched, "start_subscription",
                          return_value={"ok": False, "error": "x"}):
            res = sched.run_once(force=True)
        self.assertEqual(len(res["tried"]), 1, "有码时闸门要放行")


class SchedulerWiringTest(unittest.TestCase):
    """闸门要真的接进流程里，不能只定义不调用。"""

    def test_start_subscription_claims_first(self):
        import inspect
        from core import auto_subscribe_scheduler as sched
        src = inspect.getsource(sched.start_subscription)
        claim_pos = src.index("claim_next_promo_code")
        cards_pos = src.index("list_cards")
        self.assertLess(claim_pos, cards_pos, "领码必须在选卡之前")

    def test_failures_release_promo(self):
        import inspect
        from core import auto_subscribe_scheduler as sched
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("_release_promo(\"支付会话终态", src)
        self.assertIn("_release_promo(\"该账号本次只拿到", src)
        self.assertIn("_release_promo(\"余额不足", src)

    def test_assigned_promo_passed_to_generate(self):
        import inspect
        from core import auto_subscribe_scheduler as sched
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("promo_assigned=promo_row", src)

    def test_run_once_reclaims_before_round(self):
        import inspect
        from core import auto_subscribe_scheduler as sched
        src = inspect.getsource(sched.run_once)
        reclaim_pos = src.index("reclaim_stale_promo_codes()")
        loop_pos = src.index("for idx, acc in enumerate(ordered)")
        self.assertLess(reclaim_pos, loop_pos, "回收必须在候选循环之前")


if __name__ == "__main__":
    unittest.main()
