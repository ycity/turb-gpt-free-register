# -*- coding: utf-8 -*-
"""支付前的余额检查与自动充值。

不碰网络、不动钱：VCC 客户端全部用假对象。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.vcc_funds import (  # noqa: E402
    InsufficientFunds, _request_no, ensure_funds, preflight, required_usd,
)


class FakeClient:
    """够用的 VccClient 替身。"""

    def __init__(self, card=0.0, account=0.0, settle=True, status="PROCESSING"):
        self._card = card
        self._account = account
        self._settle = settle
        self._status = status
        self.recharges = []

    @staticmethod
    def _available(row):
        return float(row.get("balance") or 0.0)

    def card_balance(self, _cid):
        return {"balance": f"{self._card:.2f}"}

    def account_balance(self, currency="USD"):
        return self._account

    def pick_card(self, **_kw):
        return {"cardId": "CARD"}

    def recharge_card(self, card_id, amount, *, request_no, currency="USD"):
        self.recharges.append((card_id, amount, request_no))
        if self._settle:
            self._card += amount
        self._account -= amount
        return {"status": self._status, "amount": f"{amount:.2f}"}

    def wait_card_balance(self, _cid, _target, timeout=0, interval=0):
        return self._card


def _cfg(**over):
    class Cfg:
        VCC_EXPECTED_AMOUNT = 1150.0
        VCC_USD_RATE = 56.0
        VCC_BALANCE_BUFFER = 1.0
        VCC_AUTO_RECHARGE = True
        VCC_RECHARGE_AMOUNT = 21.0
        VCC_RECHARGE_TIMEOUT = 5.0
    for k, v in over.items():
        setattr(Cfg, k, v)
    return Cfg()


class RequiredUsdTest(unittest.TestCase):
    def test_converts_egp_with_buffer(self):
        # 1150 / 56 = 20.54，加 1.0 余量
        self.assertAlmostEqual(required_usd(_cfg()), 21.54, places=2)

    def test_bad_rate_falls_back(self):
        """汇率配成 0 不能导致除零或判定所有卡都够。"""
        self.assertGreater(required_usd(_cfg(VCC_USD_RATE=0)), 0)


class RequestNoTest(unittest.TestCase):
    """请求号必须可复现：接口靠它幂等，换号重试会真的扣两笔。"""

    def test_same_inputs_same_number(self):
        a = _request_no("CARD1", 21.0)
        b = _request_no("CARD1", 21.0)
        self.assertEqual(a, b)

    def test_different_card_or_amount_differs(self):
        base = _request_no("CARD1", 21.0)
        self.assertNotEqual(base, _request_no("CARD2", 21.0))
        self.assertNotEqual(base, _request_no("CARD1", 50.0))

    def test_has_recognizable_prefix(self):
        self.assertTrue(_request_no("C", 1.0).startswith("autopay_"))


class EnsureFundsTest(unittest.TestCase):
    def test_enough_balance_skips_recharge(self):
        client = FakeClient(card=50.0, account=100.0)
        r = ensure_funds(client, "CARD", _cfg())
        self.assertTrue(r.ok)
        self.assertEqual(client.recharges, [])
        self.assertEqual(r.recharged, 0.0)

    def test_recharges_when_card_low(self):
        client = FakeClient(card=1.32, account=300.0)
        r = ensure_funds(client, "CARD", _cfg())
        self.assertTrue(r.ok)
        self.assertEqual(len(client.recharges), 1)
        self.assertEqual(client.recharges[0][1], 21.0)

    def test_recharge_covers_shortfall_when_default_too_small(self):
        """默认充值额不够时按整数倍充，不能充完还是不足。"""
        client = FakeClient(card=0.0, account=500.0)
        cfg = _cfg(VCC_EXPECTED_AMOUNT=5600.0, VCC_RECHARGE_AMOUNT=21.0)
        ensure_funds(client, "CARD", cfg)
        need = required_usd(cfg)          # 5600/56 + 1 = 101.0
        amount = client.recharges[0][1]
        self.assertGreaterEqual(amount, need)
        self.assertAlmostEqual(amount % 21.0, 0.0, places=2,
                               msg="应按配置额度的整数倍充值")

    def test_recharge_uses_configured_amount_not_odd_number(self):
        """卡里有零头时也要充整额，不能算出 21.22 这种数字。"""
        client = FakeClient(card=1.32, account=300.0)
        ensure_funds(client, "CARD", _cfg())
        self.assertEqual(client.recharges[0][1], 21.0)

    def test_account_too_low_raises_before_anything(self):
        client = FakeClient(card=1.0, account=5.0)
        with self.assertRaises(InsufficientFunds) as ctx:
            ensure_funds(client, "CARD", _cfg())
        self.assertIn("账户可用余额", str(ctx.exception))
        self.assertEqual(client.recharges, [], "账户不足时不该发起充值")

    def test_auto_recharge_disabled_raises(self):
        client = FakeClient(card=1.0, account=300.0)
        with self.assertRaises(InsufficientFunds):
            ensure_funds(client, "CARD", _cfg(VCC_AUTO_RECHARGE=False))
        self.assertEqual(client.recharges, [])

    def test_recharge_not_settled_raises_without_retry(self):
        """充了但没到账要报错并提示别重发，不能静默继续。"""
        client = FakeClient(card=1.0, account=300.0, settle=False)
        with self.assertRaises(InsufficientFunds) as ctx:
            ensure_funds(client, "CARD", _cfg())
        msg = str(ctx.exception)
        self.assertIn("未到账", msg)
        self.assertIn("不要重复发起", msg)
        self.assertEqual(len(client.recharges), 1)

    def test_rejected_status_raises(self):
        client = FakeClient(card=1.0, account=300.0, status="FAILED")
        with self.assertRaises(InsufficientFunds):
            ensure_funds(client, "CARD", _cfg())


class PreflightTest(unittest.TestCase):
    """只查不充，用于生成长链前的拦截。"""

    def test_enough_reports_no_recharge(self):
        r = preflight(FakeClient(card=50.0, account=100.0), "C", _cfg())
        self.assertTrue(r["ok"])
        self.assertFalse(r["need_recharge"])

    def test_low_card_but_enough_account_is_ok(self):
        r = preflight(FakeClient(card=1.0, account=300.0), "C", _cfg())
        self.assertTrue(r["ok"])
        self.assertTrue(r["need_recharge"])
        self.assertEqual(r["recharge"], 21.0)

    def test_both_low_blocks(self):
        r = preflight(FakeClient(card=1.0, account=5.0), "C", _cfg())
        self.assertFalse(r["ok"])
        self.assertIn("请先给 VCC 账户充值", r["error"])

    def test_preflight_never_recharges(self):
        client = FakeClient(card=1.0, account=300.0)
        preflight(client, "C", _cfg())
        self.assertEqual(client.recharges, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
