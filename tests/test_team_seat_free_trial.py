# -*- coding: utf-8 -*-
"""Team 席位列 + 首月免费（结账价格为 0）检测测试。

需求：
  1. 席位探测时同时检查 checkout 支付价格是不是 0，为 0 则标识为首月免费
     （首月最多 5 席免费体验资格）；读不到价格时不能误标为 0。
  2. 账号表格新增独立「Team席位」列，把「1席/2席」和「首月免费」统一放进该列。
"""
from __future__ import annotations

import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import db, seat_warmup_service, single_seat  # noqa: E402

HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
DB_SRC = (ROOT / "core" / "db.py").read_text(encoding="utf-8")


class PriceParserTest(unittest.TestCase):
    def test_minor_units_zero_marks_free_trial(self):
        detail = {
            "business_minimum_seats": 2,
            "billing_details": {"country": "US", "currency": "USD"},
            "checkout_state": {
                "currency": "usd",
                "total": {
                    "subtotal": {"minorUnitsAmount": 5000, "amount": None},
                    "discount": {"minorUnitsAmount": 5000, "amount": None},
                    "total": {"minorUnitsAmount": 0, "amount": None},
                },
            },
        }
        got = single_seat.parse_checkout_pricing(detail)
        self.assertTrue(got["free_trial"])
        self.assertEqual(got["checkout_total_minor"], 0)
        self.assertEqual(got["checkout_total_amount"], 0.0)
        self.assertEqual(got["checkout_currency"], "USD")

    def test_minor_units_positive_is_not_free(self):
        detail = {
            "business_minimum_seats": 1,
            "checkout_state": {
                "currency": "sgd",
                "total": {
                    "total": {"minorUnitsAmount": 6976, "amount": None},
                },
            },
        }
        got = single_seat.parse_checkout_pricing(detail)
        self.assertFalse(got["free_trial"])
        self.assertEqual(got["checkout_total_minor"], 6976)
        self.assertEqual(got["checkout_total_amount"], 69.76)
        self.assertEqual(got["checkout_currency"], "SGD")

    def test_amount_string_fallback_zero_and_nonzero(self):
        zero_detail = {
            "checkout_state": {
                "currency": "usd",
                "total": {"total": {"minorUnitsAmount": None, "amount": "$0.00"}},
            }
        }
        got0 = single_seat.parse_checkout_pricing(zero_detail)
        self.assertTrue(got0["free_trial"])
        self.assertEqual(got0["checkout_total_minor"], 0)

        eu_zero = {
            "checkout_state": {
                "currency": "eur",
                "total": {"total": {"minorUnitsAmount": None, "amount": "€0,00"}},
            }
        }
        self.assertTrue(single_seat.parse_checkout_pricing(eu_zero)["free_trial"])

        thousands = {
            "checkout_state": {
                "currency": "egp",
                "total": {"total": {"minorUnitsAmount": None, "amount": "EGP 1,150.00"}},
            }
        }
        got_egp = single_seat.parse_checkout_pricing(thousands)
        self.assertFalse(got_egp["free_trial"])
        self.assertEqual(got_egp["checkout_total_amount"], 1150.0)

    def test_zero_decimal_currency(self):
        jpy_detail = {
            "checkout_state": {
                "currency": "jpy",
                "total": {"total": {"minorUnitsAmount": 4500, "amount": None}},
            }
        }
        got = single_seat.parse_checkout_pricing(jpy_detail)
        self.assertFalse(got["free_trial"])
        self.assertEqual(got["checkout_total_amount"], 4500.0)

    def test_missing_price_returns_none_never_false_positive_free(self):
        """读不到支付价格时 free_trial 必须为 None，绝不能误判成 0 元免费。"""
        for bad in (
            {},
            {"checkout_state": {}},
            {"checkout_state": {"total": {"total": {"minorUnitsAmount": None, "amount": None}}}},
            {"checkout_state": {"total": {"total": {"minorUnitsAmount": None, "amount": ""}}}},
        ):
            got = single_seat.parse_checkout_pricing(bad)
            self.assertIsNone(got["free_trial"], bad)
            self.assertIsNone(got["checkout_total_minor"], bad)

    def test_falls_back_to_create_response_pricing(self):
        detail = {"business_minimum_seats": 1}
        created = {
            "checkout_state": {
                "currency": "usd",
                "total": {"total": {"minorUnitsAmount": 0, "amount": None}},
            }
        }
        got = single_seat.parse_checkout_pricing(detail, fallback=created)
        self.assertTrue(got["free_trial"])
        self.assertEqual(got["checkout_total_minor"], 0)
        self.assertEqual(got["checkout_currency"], "USD")


class ProbeSingleSeatPriceIntegrationTest(unittest.TestCase):
    def _run_probe(self, create_json: dict, detail_json: dict):
        class FakeResp:
            def __init__(self, payload, status_code=200):
                self._p = payload
                self.status_code = status_code
                self.text = json.dumps(payload)

            def json(self):
                return self._p

        class FakeEnv:
            exit_geo = {"country": "US"}
            blocked_until = 0.0
            blocked_reason = ""

            def __init__(self, **_kw):
                pass

            def get_chatgpt_navigate_headers(self, **_kw):
                return {}

            def get_chatgpt_headers(self, **_kw):
                return {}

            def get(self, url, **_kw):
                if "payments/checkout/openai_llc" in url:
                    return FakeResp(detail_json)
                return FakeResp({})

            def post(self, url, **_kw):
                return FakeResp(create_json)

            def close(self):
                pass

        acc = {"id": 42, "email": "a@e.test", "access_token": "tok"}
        with patch("core.chatgpt_plan.normalize_token", return_value="tok"), \
             patch("core.chatgpt_plan.token_claims", return_value={"token_expired": False}), \
             patch("core.region_proxy.adopt_pool_exit", return_value=("", "")), \
             patch("core.session.BrowserSession", FakeEnv):
            return single_seat.probe_single_seat(acc)

    def test_probe_detects_single_seat_and_zero_price_free_trial(self):
        res = self._run_probe(
            {"checkout_session_id": "oaics_1"},
            {
                "business_minimum_seats": 1,
                "checkout_state": {
                    "currency": "usd",
                    "total": {"total": {"minorUnitsAmount": 0, "amount": None}},
                },
            },
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["supported"])
        self.assertEqual(res["minimum_seats"], 1)
        self.assertTrue(res["free_trial"])
        self.assertEqual(res["checkout_total_minor"], 0)
        self.assertEqual(res["checkout_total_amount"], 0.0)
        self.assertEqual(res["checkout_currency"], "USD")

    def test_probe_detects_two_seat_with_zero_price_free_trial(self):
        res = self._run_probe(
            {"checkout_session_id": "oaics_2"},
            {
                "business_minimum_seats": 2,
                "checkout_state": {
                    "currency": "usd",
                    "total": {"total": {"minorUnitsAmount": 0, "amount": None}},
                },
            },
        )
        self.assertTrue(res["ok"])
        self.assertFalse(res["supported"])
        self.assertEqual(res["minimum_seats"], 2)
        self.assertTrue(res["free_trial"])

    def test_probe_detects_paid_price_not_free(self):
        res = self._run_probe(
            {"checkout_session_id": "oaics_3"},
            {
                "business_minimum_seats": 1,
                "checkout_state": {
                    "currency": "usd",
                    "total": {"total": {"minorUnitsAmount": 5000, "amount": None}},
                },
            },
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["supported"])
        self.assertFalse(res["free_trial"])
        self.assertEqual(res["checkout_total_minor"], 5000)
        self.assertEqual(res["checkout_total_amount"], 50.0)


class DbAndWarmupPersistenceTest(unittest.TestCase):
    def test_update_writes_free_trial_and_price_on_ok(self):
        rows = [{"id": 7}]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts") as save:
            ok = db.update_account_single_seat(7, {
                "ok": True,
                "supported": True,
                "minimum_seats": 1,
                "free_trial": True,
                "checkout_total_minor": 0,
                "checkout_total_amount": 0.0,
                "checkout_currency": "USD",
            })
        self.assertTrue(ok)
        self.assertTrue(rows[0]["team_free_trial"])
        self.assertEqual(rows[0]["team_checkout_total_minor"], 0)
        self.assertEqual(rows[0]["team_checkout_total_amount"], 0.0)
        self.assertEqual(rows[0]["team_checkout_currency"], "USD")
        save.assert_called_once()

    def test_update_failure_does_not_touch_free_trial(self):
        rows = [{"id": 7, "team_free_trial": True}]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts"):
            db.update_account_single_seat(7, {"ok": False, "error": "timeout"})
        self.assertTrue(rows[0]["team_free_trial"])

    def test_fields_exposed_in_db_and_app(self):
        for field in (
            "team_free_trial",
            "team_checkout_total_minor",
            "team_checkout_total_amount",
            "team_checkout_currency",
        ):
            self.assertIn(field, DB_SRC, f"db.py 缺 {field}")
            self.assertIn(field, APP, f"app.py 缺 {field}")

    def test_seat_filter_supports_free_trial(self):
        self.assertTrue(db._matches_seat_filter({"id": 1, "team_free_trial": True}, "free_trial"))
        self.assertFalse(db._matches_seat_filter({"id": 2, "team_free_trial": False}, "free_trial"))
        self.assertFalse(db._matches_seat_filter({"id": 3}, "free_trial"))
        where, _ = db._account_filter_sql(seat_filter="free_trial")
        self.assertTrue(any("team_free_trial" in w for w in where))

    def test_warmup_rotates_2fa_for_free_trial_accounts_too(self):
        self.assertTrue(seat_warmup_service._probe_ok({"ok": True, "supported": False, "free_trial": True}))
        self.assertTrue(seat_warmup_service._probe_ok({"ok": True, "supported": True, "free_trial": False}))
        self.assertFalse(seat_warmup_service._probe_ok({"ok": True, "supported": False, "free_trial": False}))


class TeamSeatColumnUiTest(unittest.TestCase):
    def _table_head(self):
        i = HTML.index('id="accountsSelectAllV2"')
        return HTML[HTML.rindex("<colgroup", 0, i):HTML.index("</thead>", i)]

    def _row(self):
        i = HTML.index("const rowHtmlV2 = (r) =>")
        return HTML[i:HTML.index("`;", i)]

    def test_team_seat_column_exists_in_header_and_colgroup(self):
        head = self._table_head()
        self.assertIn('<col class="col-seat">', head)
        self.assertIn(">Team席位</th>", head)

    def test_team_seat_column_rendered_in_row(self):
        row = self._row()
        self.assertIn('<td class="col-seat">${_teamSeatCellV2(r)}</td>', row)

    def test_free_trial_badge_and_styles_present(self):
        self.assertIn("function _teamFreeTrialTag", HTML)
        self.assertIn("首月免费", HTML)
        self.assertIn(".accounts-table-v2 .pill.status-free-trial", HTML)
        self.assertIn(".accounts-table-v2 .col-seat { width:", HTML)

    def test_column_counts_and_colspan_match(self):
        head = self._table_head()
        cols = len(re.findall(r"<col ", head))
        ths = len(re.findall(r"<th ", head))
        tds = len(re.findall(r"^\s*<td ", self._row(), re.M))
        i = HTML.index("暂无账号")
        m = re.search(r'colspan="(\d+)"', HTML[max(0, i - 200):i])
        self.assertEqual(cols, ths)
        self.assertEqual(ths, tds)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), cols)


if __name__ == "__main__":
    unittest.main()
