# -*- coding: utf-8 -*-
"""账户低余额（< 18 USD）时的选卡优先策略测试。

需求：
  订阅时，如果 VCC 账户余额低于 18 USD，选卡应优先从余额充足（>= 18 USD）
  的卡片里选择；在这些余额充足的卡片里，再选择使用次数最少的。
  账户余额 >= 18 USD 时保持原有全量均衡策略，分散单卡用量。
"""
from __future__ import annotations

import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import auto_subscribe_scheduler as sched  # noqa: E402
from core.vcc_client import VccError  # noqa: E402


def _card(tail: str, bal: float | str, status: str = "ACTIVE") -> dict:
    return {
        "cardId": f"cid-{tail}",
        "cardNoMask": f"537872****{tail}",
        "balance": bal,
        "cardStatus": status,
    }


class ConfigAndHelpersTest(unittest.TestCase):
    def test_default_threshold_is_18(self):
        from config import auto_subscribe as cfg
        self.assertEqual(cfg.AUTO_SUBSCRIBE_LOW_BALANCE_USD, 18.0)
        with patch.object(sched, "_cfg", lambda n, d: d):
            self.assertEqual(sched._low_balance_threshold(), 18.0)

    def test_env_override_registered(self):
        src = (ROOT / "config" / "auto_subscribe.py").read_text(encoding="utf-8")
        self.assertIn("'AUTO_SUBSCRIBE_LOW_BALANCE_USD': 'float'", src)

    def test_card_balance_parsing(self):
        self.assertEqual(sched._card_balance({"balance": 23.5}), 23.5)
        self.assertEqual(sched._card_balance({"balance": "19.25"}), 19.25)
        self.assertEqual(sched._card_balance({"availableBalance": "21.0", "balance": "1.0"}), 21.0)
        self.assertEqual(sched._card_balance({"availableAmount": 18.0}), 18.0)
        self.assertEqual(sched._card_balance({"balance": None}), 0.0)
        self.assertEqual(sched._card_balance({"balance": "bad"}), 0.0)
        self.assertEqual(sched._card_balance({}), 0.0)

    def test_query_account_balance_handles_types_and_errors(self):
        c1 = types.SimpleNamespace(account_balance=lambda: 12.34)
        self.assertEqual(sched._query_account_balance(c1), 12.34)

        c2 = types.SimpleNamespace(account_balance=lambda: "0.00")
        self.assertEqual(sched._query_account_balance(c2), 0.0)

        c3 = types.SimpleNamespace(account_balance=lambda: Decimal("17.99"))
        self.assertEqual(sched._query_account_balance(c3), 17.99)

        c_err = MagicMock()
        c_err.account_balance.side_effect = VccError("boom")
        self.assertIsNone(sched._query_account_balance(c_err))

        # 未显式配置 return_value 的 MagicMock 不能误当作 1.0
        c_unconfigured = MagicMock()
        self.assertIsNone(sched._query_account_balance(c_unconfigured))

        c_bool = types.SimpleNamespace(account_balance=lambda: False)
        self.assertIsNone(sched._query_account_balance(c_bool))

        c_no_method = types.SimpleNamespace()
        self.assertIsNone(sched._query_account_balance(c_no_method))

    def test_status_exposes_threshold(self):
        with patch.object(sched, "find_candidates", return_value=[]), \
             patch.object(sched, "card_usage", return_value={}):
            st = sched.status()
        self.assertIn("low_balance_threshold_usd", st)
        self.assertEqual(st["low_balance_threshold_usd"], sched._low_balance_threshold())


class PickBalancedLowBalanceTest(unittest.TestCase):
    def test_prefers_sufficient_cards_over_unused_low_balance_card(self):
        """账户低余额时：哪怕低余额卡用过 0 次，也必须优先选余额 >= 18 且使用最少的卡。"""
        cards = [
            _card("1111", 3.50),   # 余额不足，使用 0 次
            _card("2222", 30.00),  # 余额充足，使用 5 次
            _card("3333", 20.00),  # 余额充足，使用 2 次 -> 应选中
        ]
        usage = {"1111": 0, "2222": 5, "3333": 2}
        with patch.object(sched, "card_usage", return_value=usage):
            for _ in range(20):
                got = sched._pick_balanced(cards, min_balance=18.0)
                self.assertEqual(got["cardId"], "cid-3333")

    def test_random_within_lowest_tier_of_sufficient_cards(self):
        """余额充足的卡里，最低使用次数并列多张时应随机分配。"""
        cards = [
            _card("1111", 5.00),   # 不足，0 次
            _card("2222", 25.00),  # 充足，1 次
            _card("3333", 19.50),  # 充足，1 次
            _card("4444", 50.00),  # 充足，4 次
        ]
        usage = {"1111": 0, "2222": 1, "3333": 1, "4444": 4}
        with patch.object(sched, "card_usage", return_value=usage):
            seen = {sched._pick_balanced(cards, min_balance=18.0)["cardId"]
                    for _ in range(50)}
        self.assertEqual(seen, {"cid-2222", "cid-3333"})

    def test_exact_18_is_sufficient_and_17_99_is_not(self):
        """与 ensure_funds (card_bal >= need) 口径一致：18.00 算充足，17.99 不算。"""
        cards = [
            _card("1111", 17.99),  # 不足，0 次
            _card("2222", 18.00),  # 充足，3 次 -> 应选中
        ]
        with patch.object(sched, "card_usage", return_value={"1111": 0, "2222": 3}):
            got = sched._pick_balanced(cards, min_balance=18.0)
        self.assertEqual(got["cardId"], "cid-2222")

    def test_falls_back_when_no_card_has_sufficient_balance(self):
        """没有任何卡余额 >= 18 时，退回全部卡片里使用次数最少的（优先而非硬拦）。"""
        cards = [
            _card("1111", 12.00),  # 使用 4 次
            _card("2222", 5.00),   # 使用 1 次 -> 应选中
        ]
        with patch.object(sched, "card_usage", return_value={"1111": 4, "2222": 1}):
            got = sched._pick_balanced(cards, min_balance=18.0)
        self.assertEqual(got["cardId"], "cid-2222")

    def test_normal_mode_ignores_card_balance(self):
        """min_balance=0（账户余额充足）时不按余额筛，纯按使用次数最少选。"""
        cards = [
            _card("1111", 2.00),   # 使用 0 次 -> 应选中
            _card("2222", 50.00),  # 使用 3 次
        ]
        with patch.object(sched, "card_usage", return_value={"1111": 0, "2222": 3}):
            got = sched._pick_balanced(cards, min_balance=0.0)
        self.assertEqual(got["cardId"], "cid-1111")


class StartSubscriptionLowBalanceIntegrationTest(unittest.TestCase):
    """端到端验证 start_subscription 的账户余额判断、选卡与坏地址重选。"""

    def _base_cfg(self):
        return types.SimpleNamespace(
            VCC_1024PROXY_MINUTES=90,
            VCC_FUNDING_USD=18.0,
            VCC_MIN_CARD_BALANCE=0.0,
            VCC_USD_RATE=56.0,
            VCC_BALANCE_BUFFER=0.5,
            VCC_AUTO_RECHARGE=True,
            VCC_RECHARGE_AMOUNT=20.0,
            VCC_CARD_ID="",
            VCC_MAX_CHARGE=0.0,
            VCC_EXPECTED_CURRENCY="",
            VCC_AUTO_CONFIRM=False,
        )

    def _run_start(self, *, cards, usage, acct_bal, addr_ok=None,
                   threshold=18.0, blacklist="5931,8361"):
        from core.vcc_pay_session import ST_DONE

        client = MagicMock()
        client.list_cards.return_value = list(cards)
        if isinstance(acct_bal, Exception):
            client.account_balance.side_effect = acct_bal
        else:
            client.account_balance.return_value = acct_bal

        chosen_cards = []

        def fake_start_session(**kw):
            chosen_cards.append(kw["cfg"].VCC_CARD_ID)
            return types.SimpleNamespace(sid="s1", state=ST_DONE,
                                         message="ok", gate_error="")

        addr_fn = addr_ok or (lambda _cl, _c: True)
        cfg_map = {
            "AUTO_SUBSCRIBE_COUNTRY": "EG",
            "AUTO_SUBSCRIBE_MAX_AMOUNT": 920.0,
            "AUTO_SUBSCRIBE_CARD_BLACKLIST": blacklist,
            "AUTO_SUBSCRIBE_LOW_BALANCE_USD": threshold,
        }
        with patch.object(sched, "_vcc_cfg", return_value=self._base_cfg()), \
             patch.object(sched, "_cfg", side_effect=lambda k, d=None: cfg_map.get(k, d)), \
             patch.object(sched, "card_usage", return_value=dict(usage)), \
             patch.object(sched, "note_card_used"), \
             patch.object(sched, "_addr_is_payable", side_effect=addr_fn), \
             patch.object(sched, "_verified_proxies_in_country", return_value=["http://1.1.1.1:1"]), \
             patch("core.db.get_account", return_value={"id": 10, "email": "a@t", "access_token": "tok"}), \
             patch("core.db.update_account_team_checkout"), \
             patch("core.team_checkout_service.claim_next_promo_code", return_value={"id": 1, "code": "P1"}), \
             patch("core.team_checkout_service.release_promo_code"), \
             patch("core.team_checkout_service.fetch_1024proxy_ips", return_value=["1.1.1.1:1"]), \
             patch("core.vcc_client.build_client_from_config", return_value=client), \
             patch("core.vcc_funds.ensure_funds", return_value=types.SimpleNamespace(recharged=0.0)), \
             patch("core.vcc_funds.required_usd", return_value=18.0), \
             patch("core.team_checkout_service.generate_team_checkout_link",
                   return_value={"ok": True, "url": "https://chatgpt.com/checkout/openai_llc/oaics_1",
                                 "seats": 1, "promo_used": "P1", "proxy_used": "http://1.1.1.1:1"}), \
             patch("core.vcc_pay_session.start_session", side_effect=fake_start_session):
            res = sched.start_subscription(10)
        return res, chosen_cards, client

    def test_low_account_balance_picks_least_used_sufficient_non_blacklisted_card(self):
        cards = [
            _card("5931", 99.00),  # 黑名单，即使有钱 0 次也要跳过
            _card("1111", 4.00),   # 余额不足，0 次
            _card("2222", 35.00),  # 余额充足，4 次
            _card("3333", 21.00),  # 余额充足，1 次 -> 应选中
        ]
        res, chosen, _ = self._run_start(
            cards=cards,
            usage={"1111": 0, "2222": 4, "3333": 1},
            acct_bal=12.50,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(chosen, ["cid-3333"])

    def test_sufficient_account_balance_uses_normal_balanced_selection(self):
        """账户余额 >= 18 USD（含刚好 18.0）时，不按卡余额筛，选使用最少的卡。"""
        cards = [
            _card("1111", 4.00),   # 使用 0 次 -> 应选中
            _card("2222", 35.00),  # 使用 2 次
        ]
        for acct_bal in (18.0, 50.0):
            res, chosen, _ = self._run_start(
                cards=cards,
                usage={"1111": 0, "2222": 2},
                acct_bal=acct_bal,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(chosen, ["cid-1111"], f"acct_bal={acct_bal}")

    def test_bad_address_retries_next_sufficient_card_before_low_balance_card(self):
        """第一张充足卡地址不可用时，必须继续试下一张充足卡，而不是跳去低余额卡。"""
        cards = [
            _card("1111", 3.00),   # 余额不足，0 次，地址正常
            _card("1588", 25.00),  # 余额充足，0 次，地址异常（先抽到它）
            _card("2222", 22.00),  # 余额充足，2 次，地址正常 -> 重试应选中它
            _card("3333", 30.00),  # 余额充足，5 次，地址正常
        ]
        probed = []

        def addr_ok(_cl, card):
            probed.append(card["cardId"])
            return card["cardId"] != "cid-1588"

        res, chosen, _ = self._run_start(
            cards=cards,
            usage={"1111": 0, "1588": 0, "2222": 2, "3333": 5},
            acct_bal=5.0,
            addr_ok=addr_ok,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(probed, ["cid-1588", "cid-2222"])
        self.assertEqual(chosen, ["cid-2222"])

    def test_bad_address_on_all_sufficient_cards_falls_back_to_remaining(self):
        """所有充足卡地址都不可用时，才退回其余可用卡。"""
        cards = [
            _card("1111", 3.00),   # 余额不足，地址正常 -> 最终退回它
            _card("1588", 25.00),  # 余额充足，地址异常
        ]
        res, chosen, _ = self._run_start(
            cards=cards,
            usage={"1111": 0, "1588": 0},
            acct_bal=5.0,
            addr_ok=lambda _cl, card: card["cardId"] != "cid-1588",
        )
        self.assertTrue(res["ok"])
        self.assertEqual(chosen, ["cid-1111"])

    def test_account_balance_error_falls_back_gracefully(self):
        """查账户余额失败不能导致订阅崩溃，应回退默认均衡选卡。"""
        cards = [
            _card("1111", 3.00),   # 使用 0 次 -> 回退默认选卡应选它
            _card("2222", 30.00),  # 使用 3 次
        ]
        res, chosen, _ = self._run_start(
            cards=cards,
            usage={"1111": 0, "2222": 3},
            acct_bal=VccError("API 500"),
        )
        self.assertTrue(res["ok"])
        self.assertEqual(chosen, ["cid-1111"])

    def test_threshold_zero_disables_account_balance_check(self):
        cards = [
            _card("1111", 3.00),
            _card("2222", 30.00),
        ]
        res, chosen, client = self._run_start(
            cards=cards,
            usage={"1111": 0, "2222": 3},
            acct_bal=0.0,
            threshold=0.0,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(chosen, ["cid-1111"])
        client.account_balance.assert_not_called()


class PickForPaymentHelperTest(unittest.TestCase):
    def test_low_balance_picks_sufficient_least_used(self):
        client = MagicMock()
        client.list_cards.return_value = [
            _card("5931", 50.0),   # 黑名单
            _card("1111", 5.0),    # 余额不足，0 次
            _card("2222", 28.0),   # 充足，3 次
            _card("3333", 19.0),   # 充足，1 次 -> 选中
        ]
        client.account_balance.return_value = 9.0
        with patch.object(sched, "_cfg", side_effect=lambda k, d=None: {
            "AUTO_SUBSCRIBE_CARD_BLACKLIST": "5931",
            "AUTO_SUBSCRIBE_LOW_BALANCE_USD": 18.0,
        }.get(k, d)), patch.object(sched, "card_usage", return_value={"1111": 0, "2222": 3, "3333": 1}):
            picked = sched.pick_vcc_card_for_payment(client)
        self.assertEqual(picked["cardId"], "cid-3333")

    def test_normal_balance_picks_least_used_overall(self):
        client = MagicMock()
        client.list_cards.return_value = [
            _card("1111", 5.0),   # 0 次 -> 选中
            _card("2222", 28.0),  # 2 次
        ]
        client.account_balance.return_value = 25.0
        with patch.object(sched, "_cfg", side_effect=lambda k, d=None: {
            "AUTO_SUBSCRIBE_CARD_BLACKLIST": "",
            "AUTO_SUBSCRIBE_LOW_BALANCE_USD": 18.0,
        }.get(k, d)), patch.object(sched, "card_usage", return_value={"1111": 0, "2222": 2}):
            picked = sched.pick_vcc_card_for_payment(client)
        self.assertEqual(picked["cardId"], "cid-1111")


class WebUiWiringTest(unittest.TestCase):
    def test_pay_cards_exposes_account_balance_and_threshold(self):
        app_src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        seg = app_src[app_src.index("def api_pay_cards"):]
        seg = seg[:seg.index("def api_pay_card_secrets")]
        self.assertIn('"account_balance"', seg)
        self.assertIn('"low_balance_threshold"', seg)
        self.assertIn("_query_account_balance", seg)

    def test_pay_start_and_funds_check_use_shared_picker(self):
        app_src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        start_seg = app_src[app_src.index("def api_account_pay_start"):]
        start_seg = start_seg[:start_seg.index("ensure_funds(")]
        self.assertIn("pick_vcc_card_for_payment", start_seg)
        self.assertIn("run_cfg.VCC_CARD_ID = card_id", start_seg)

        funds_seg = app_src[app_src.index("def api_pay_funds_check"):]
        funds_seg = funds_seg[:funds_seg.index("preflight(")]
        self.assertIn("pick_vcc_card_for_payment", funds_seg)

    def test_frontend_prefers_sufficient_least_used_on_low_account_balance(self):
        html = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        seg = html[html.index("// 选卡优先级"):html.index("await _autoPayCheckFunds")]
        self.assertIn("_autoPayAccountBalance", seg)
        self.assertIn("_autoPayLowBalanceThreshold", seg)
        self.assertIn("_lowBalPick", seg)
        self.assertIn("auto_sub_used", seg)
        self.assertIn("auto_sub_blacklist", seg)


if __name__ == "__main__":
    unittest.main()
