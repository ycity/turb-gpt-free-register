# -*- coding: utf-8 -*-
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountPlanFilterTests(unittest.TestCase):
    def test_other_discount_sql_and_python_agree(self):
        def campaign(value):
            return {"metadata": {"discount": {"percentage": value}}}

        cases = [
            ({"plus": campaign(25)}, True),
            ({"plus": campaign(75.5)}, True),
            ({"plus": campaign(50)}, False),
            ({"plus": campaign(100)}, False),
            ({"plus": campaign(0)}, False),
            ({"plus": campaign(-10)}, False),
            ({"plus": campaign(101)}, False),
            ({"plus": campaign(None)}, False),
            ({"plus": campaign(True)}, False),
            ({"plus": campaign("unknown")}, False),
            ({"plus": {}}, False),
            ({}, False),
            ({"plus": campaign(50), "go": campaign(25)}, True),
        ]
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE accounts (payload TEXT)")
            for campaigns, expected in cases:
                row = {"current_plan_type": "free", "eligible_promo_campaigns": campaigns}
                conn.execute("DELETE FROM accounts")
                conn.execute("INSERT INTO accounts VALUES (?)", (json.dumps(row),))
                for filter_name in ("promo:*:other", "promo::other", "promo:plus:other", "promo:go:other"):
                    with self.subTest(campaigns=campaigns, filter=filter_name):
                        where, params = db._account_filter_sql(plan_filter=filter_name)
                        sql_match = bool(conn.execute(
                            "SELECT COUNT(*) FROM accounts WHERE " + " AND ".join(where), params
                        ).fetchone()[0])
                        python_match = db._account_matches_plan_filter(row, filter_name)
                        self.assertEqual(sql_match, python_match)
                        if filter_name in ("promo:*:other", "promo::other"):
                            self.assertEqual(sql_match, expected)
                        if campaigns == {"plus": campaign(50), "go": campaign(25)}:
                            self.assertEqual(sql_match, filter_name != "promo:plus:other")

    def test_plus_trial_and_no_trial_filters_use_sql(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            accounts_path.write_text(json.dumps([
                {"id": 1, "email": "eligible@example.com", "current_plan_type": "free", "plus_trial_eligible": True},
                {"id": 2, "email": "not-eligible@example.com", "current_plan_type": "free", "plus_trial_eligible": False,
                 "eligible_promo_campaigns": {}},
                {"id": 3, "email": "paid@example.com", "current_plan_type": "plus", "plus_trial_eligible": True},
                {"id": 4, "email": "missing-plan@example.com", "plus_trial_eligible": True},
                {"id": 5, "email": "unknown-eligibility@example.com", "current_plan_type": "free"},
                {"id": 6, "email": "go-promo@example.com", "current_plan_type": "free", "plus_trial_eligible": False,
                 "eligible_promo_campaigns": {"go": {"id": "go-free"}}},
                {"id": 7, "email": "plus-promo@example.com", "current_plan_type": "free", "plus_trial_eligible": True,
                 "eligible_promo_campaigns": {"plus": {"id": "plus-free", "metadata": {"discount": {"percentage": 100}}}}},
                {"id": 8, "email": "paid-promo@example.com", "current_plan_type": "pro",
                 "eligible_promo_campaigns": {"business": {"id": "business-offer"}}},
                {"id": 9, "email": "business-promo@example.com", "current_plan_type": "free",
                 "eligible_promo_campaigns": {"business": {"id": "business-offer", "metadata": {"plan_name": "chatgptbusinessplan"}}}},
                {"id": 10, "email": "mixed-promo@example.com", "current_plan_type": "free",
                 "eligible_promo_campaigns": {
                     "plus": {"metadata": {"discount": {"percentage": 50}}},
                     "go": {"metadata": {"discount": {"percentage": 100}}}
                 }},
            ], ensure_ascii=False), encoding="utf-8")

            missing = root / "missing.json"
            with patch.multiple(
                db,
                _ACCOUNTS_JSON=accounts_path,
                _LEGACY_ACCOUNTS_JSON=missing,
                _OUTLOOK_JSON=missing,
                _GENERIC_API_EMAIL_JSON=missing,
                _JOBS_JSON=missing,
                _DOMAIN_EMAIL_JSON=missing,
            ), patch.object(db, "_SQLITE_READY", False), patch.object(db, "_SQLITE_READY_PATH", None):
                for filter_name in ("plus_trial", "plus_trial_eligible", "trial"):
                    result = db.list_accounts_page(limit=20, plan_filter=filter_name)
                    self.assertEqual([item["id"] for item in result["items"]], [7, 1])

                promo_result = db.list_accounts_page(limit=20, plan_filter="promo")
                self.assertEqual([item["id"] for item in promo_result["items"]], [10, 9, 7, 6])
                self.assertEqual([item["id"] for item in db.list_accounts_page(limit=20, plan_filter="promo:go")["items"]], [10, 6])
                self.assertEqual([item["id"] for item in db.list_accounts_page(limit=20, plan_filter="promo:plus")["items"]], [10, 7])
                self.assertEqual([item["id"] for item in db.list_accounts_page(limit=20, plan_filter="promo:business")["items"]], [9])
                self.assertEqual([item["id"] for item in db.list_accounts_page(limit=20, plan_filter="promo:*:100")["items"]], [10, 7])
                self.assertEqual([item["id"] for item in db.list_accounts_page(limit=20, plan_filter="promo:go:100")["items"]], [10])
                self.assertEqual([item["id"] for item in db.list_accounts_page(limit=20, plan_filter="promo:plus:100")["items"]], [7])

                free_result = db.list_accounts_page(limit=20, plan_filter="free")
                self.assertEqual([item["id"] for item in free_result["items"]], [10, 9, 7, 6, 5, 2, 1])

                no_trial_result = db.list_accounts_page(limit=20, plan_filter="free_no_trial")
                self.assertEqual([item["id"] for item in no_trial_result["items"]], [2])

                snapshot = db.list_account_plan_check_statuses(limit=20, plan_filter="plus_trial")
                self.assertEqual([item["id"] for item in snapshot["items"]], [7, 1])

                promo_snapshot = db.list_account_plan_check_statuses(limit=20, plan_filter="promo")
                self.assertEqual([item["id"] for item in promo_snapshot["items"]], [10, 9, 7, 6])
                typed_snapshot = db.list_account_plan_check_statuses(limit=20, plan_filter="promo:business")
                self.assertEqual([item["id"] for item in typed_snapshot["items"]], [9])

                free_snapshot = db.list_account_plan_check_statuses(limit=20, plan_filter="free")
                self.assertEqual([item["id"] for item in free_snapshot["items"]], [10, 9, 7, 6, 5, 2, 1])

                no_trial_snapshot = db.list_account_plan_check_statuses(limit=20, plan_filter="free_no_trial")
                self.assertEqual([item["id"] for item in no_trial_snapshot["items"]], [2])

    def test_subscribed_filter_covers_team_not_just_plus(self):
        """线上事故：筛选写死 "plus" in plan，36 个 Team 号一个都筛不出来。

        库里实际只有 free / team 两种取值（2026-11-17 实测），
        "已订阅" 必须把 team 也算进去。
        """
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE accounts (payload TEXT)")
            cases = [
                ("team", True),
                ("plus", True),
                ("pro", True),
                ("go", True),
                ("business", True),
                ("chatgptteamplan", True),
                ("free", False),
                # “free(可Plus试用)”只是资格，不算已订阅
                ("free(可plus试用)", False),
                # 没查过套餐，状态未知
                ("", False),
            ]
            for plan, expected in cases:
                row = {"current_plan_type": plan}
                conn.execute("DELETE FROM accounts")
                conn.execute("INSERT INTO accounts VALUES (?)", (json.dumps(row),))
                for filter_name in ("subscribed", "plus"):
                    with self.subTest(plan=plan, filter=filter_name):
                        where, params = db._account_filter_sql(plan_filter=filter_name)
                        sql_match = bool(conn.execute(
                            "SELECT COUNT(*) FROM accounts WHERE " + " AND ".join(where), params
                        ).fetchone()[0])
                        py_match = db._account_matches_plan_filter(row, filter_name)
                        self.assertEqual(sql_match, py_match, "SQL 与 Python 口径必须一致")
                        self.assertEqual(sql_match, expected)

    def test_unsubscribed_filter_excludes_paid_and_unknown(self):
        """未订阅：查过套餐且不是付费套餐。没查过的不算。"""
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE accounts (payload TEXT)")
            cases = [
                ("free", True),
                ("free(可plus试用)", True),
                ("team", False),
                ("plus", False),
                ("pro", False),
                ("go", False),
                ("business", False),
                # 没查过套餐，状态未知，不该混进“未订阅”
                ("", False),
            ]
            for plan, expected in cases:
                row = {"current_plan_type": plan}
                conn.execute("DELETE FROM accounts")
                conn.execute("INSERT INTO accounts VALUES (?)", (json.dumps(row),))
                for filter_name in ("unsubscribed", "not_subscribed"):
                    with self.subTest(plan=plan, filter=filter_name):
                        where, params = db._account_filter_sql(plan_filter=filter_name)
                        sql_match = bool(conn.execute(
                            "SELECT COUNT(*) FROM accounts WHERE " + " AND ".join(where), params
                        ).fetchone()[0])
                        py_match = db._account_matches_plan_filter(row, filter_name)
                        self.assertEqual(sql_match, py_match, "SQL 与 Python 口径必须一致")
                        self.assertEqual(sql_match, expected)

    def test_subscribed_and_unsubscribed_partition_known_plans(self):
        """已订阅 / 未订阅 必须互补：查过套餐的账号恰好落入其一，不重不漏。"""
        for plan in ("free", "team", "plus", "pro", "go", "business",
                     "chatgptteamplan", "free(可plus试用)"):
            row = {"current_plan_type": plan}
            sub = db._account_matches_plan_filter(row, "subscribed")
            unsub = db._account_matches_plan_filter(row, "unsubscribed")
            with self.subTest(plan=plan):
                self.assertNotEqual(sub, unsub, f"{plan} 同时命中或同时落空")

    def test_unsubscribed_uses_sql_on_real_pipeline(self):
        """走完整的 list_accounts_page，确认筛选真的下推生效。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            accounts_path.write_text(json.dumps([
                {"id": 1, "email": "t@example.com", "current_plan_type": "team"},
                {"id": 2, "email": "f@example.com", "current_plan_type": "free"},
                {"id": 3, "email": "p@example.com", "current_plan_type": "plus"},
                {"id": 4, "email": "u@example.com"},
            ], ensure_ascii=False), encoding="utf-8")
            missing = root / "missing.json"
            with patch.multiple(
                db,
                _ACCOUNTS_JSON=accounts_path,
                _LEGACY_ACCOUNTS_JSON=missing,
                _OUTLOOK_JSON=missing,
                _GENERIC_API_EMAIL_JSON=missing,
                _JOBS_JSON=missing,
                _DOMAIN_EMAIL_JSON=missing,
            ), patch.object(db, "_SQLITE_READY", False), patch.object(db, "_SQLITE_READY_PATH", None):
                sub = db.list_accounts_page(limit=20, plan_filter="subscribed")
                self.assertEqual(sorted(i["id"] for i in sub["items"]), [1, 3])
                unsub = db.list_accounts_page(limit=20, plan_filter="unsubscribed")
                self.assertEqual([i["id"] for i in unsub["items"]], [2])


if __name__ == "__main__":
    unittest.main()
