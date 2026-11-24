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

    def __init__(self, card=0.0, account=0.0, settle=True, status="PROCESSING",
                 idempotent_hits=()):
        self._card = card
        self._account = account
        self._settle = settle
        self._status = status
        # 这些请求号会被当成"已处理过的旧单"：返回成功但不划钱，复刻 VCC 的
        # 幂等行为（2026-11-02 线上就是被自己算出的重复号拦住的）。
        self._idempotent_hits = set(idempotent_hits)
        self.recharges = []
        self.seen_request_nos = []

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
        self.seen_request_nos.append(request_no)
        if request_no in self._idempotent_hits:
            # 幂等命中：接口报成功，钱一分不动。
            return {"status": "SUCCESS", "amount": f"{amount:.2f}"}
        if self._settle:
            self._card += amount
        self._account -= amount
        return {"status": self._status, "amount": f"{amount:.2f}"}

    def wait_card_balance(self, _cid, _target, timeout=0, interval=0):
        return self._card


def _cfg(**over):
    class Cfg:
        # 闸门默认关，充值走备款额
        VCC_EXPECTED_AMOUNT = 0.0
        # EGP 折算那条路的参数；默认用它，想测 USD 直配就传 VCC_FUNDING_USD
        VCC_FUNDING_USD = 0.0
        VCC_FUNDING_AMOUNT = 1150.0
        VCC_USD_RATE = 56.0
        VCC_BALANCE_BUFFER = 1.0
        VCC_AUTO_RECHARGE = True
        VCC_RECHARGE_AMOUNT = 35.0
        VCC_RECHARGE_TIMEOUT = 5.0
    for k, v in over.items():
        setattr(Cfg, k, v)
    return Cfg()


class RequiredUsdTest(unittest.TestCase):
    def test_converts_egp_with_buffer(self):
        # 1150 / 56 = 20.54，加 1.0 余量
        self.assertAlmostEqual(required_usd(_cfg()), 21.54, places=2)

    def test_direct_usd_wins_over_egp_conversion(self):
        """配了 USD 备款额就直接用它，不再按汇率折算。

        US 区单席位账单本来就是美元，折算只会算出个不相干的数
        （线上 1932/56+0.5=35，而实际只需 18）。
        """
        cfg = _cfg(VCC_FUNDING_USD=18.0, VCC_FUNDING_AMOUNT=1932.0)
        self.assertEqual(required_usd(cfg), 18.0)

    def test_direct_usd_ignores_rate_and_buffer(self):
        """USD 直配不该再叠汇率或余量，否则配 18 实际要 18+buffer。"""
        cfg = _cfg(VCC_FUNDING_USD=18.0, VCC_USD_RATE=3.0,
                   VCC_BALANCE_BUFFER=9.0)
        self.assertEqual(required_usd(cfg), 18.0)

    def test_zero_direct_usd_falls_back_to_egp(self):
        """USD 备款额置 0 要退回 EGP 折算，这是 EG 区那条路的开关。"""
        cfg = _cfg(VCC_FUNDING_USD=0.0, VCC_FUNDING_AMOUNT=1150.0)
        self.assertAlmostEqual(required_usd(cfg), 21.54, places=2)

    def test_legacy_config_without_direct_usd(self):
        """老配置没有 VCC_FUNDING_USD 字段时不能报错，按 EGP 折算走。"""
        class Legacy:
            VCC_EXPECTED_AMOUNT = 0.0
            VCC_FUNDING_AMOUNT = 1150.0
            VCC_USD_RATE = 56.0
            VCC_BALANCE_BUFFER = 1.0
        self.assertAlmostEqual(required_usd(Legacy()), 21.54, places=2)

    def test_bad_rate_falls_back(self):
        """汇率配成 0 不能导致除零或判定所有卡都够。"""
        self.assertGreater(required_usd(_cfg(VCC_USD_RATE=0)), 0)

    def test_gate_amount_does_not_drive_funding(self):
        """关掉等值校验（置 0）不能让充值金额也变 0，否则卡不充值、支付必失败。"""
        cfg = _cfg(VCC_EXPECTED_AMOUNT=0.0, VCC_FUNDING_AMOUNT=1150.0)
        self.assertAlmostEqual(required_usd(cfg), 21.54, places=2)

    def test_falls_back_to_gate_for_legacy_config(self):
        """老配置没有 VCC_FUNDING_AMOUNT 时回退到闸门值，不能算成 0。"""
        class Legacy:
            VCC_EXPECTED_AMOUNT = 1150.0
            VCC_USD_RATE = 56.0
            VCC_BALANCE_BUFFER = 1.0
        self.assertAlmostEqual(required_usd(Legacy()), 21.54, places=2)


class RechargeAmountConfigTest(unittest.TestCase):
    def test_configured_amount(self):
        from config import vcc
        self.assertEqual(vcc.VCC_RECHARGE_AMOUNT, 20.0)

    def test_fallback_matches_config(self):
        """配置缺失时的兜底值必须和配置一致，否则实际充的钱和配置写的不一样。"""
        from config import vcc
        from core.vcc_funds import _DEFAULT_TOP_UP
        self.assertEqual(_DEFAULT_TOP_UP, vcc.VCC_RECHARGE_AMOUNT)

    def test_zero_config_uses_fallback_not_zero(self):
        """充值额配成 0 不能导致充 0 块钱、死循环或永远不足。

        不写死具体数字：兜底额按整数倍凑够需求，倍数随备款额变。
        要盯的是"不是 0、够付、且是兜底额的整数倍"。
        """
        from core.vcc_funds import _DEFAULT_TOP_UP
        client = FakeClient(card=0.0, account=300.0)
        cfg = _cfg(VCC_RECHARGE_AMOUNT=0)
        ensure_funds(client, "CARD", cfg)
        amount = client.recharges[0][1]
        self.assertGreater(amount, 0.0, "不能充 0 块钱")
        self.assertGreaterEqual(amount, required_usd(cfg), "充完还是不够")
        self.assertAlmostEqual(amount % _DEFAULT_TOP_UP, 0.0, places=2,
                               msg="应按兜底额的整数倍充值")

    def test_one_top_up_covers_a_normal_bill(self):
        """一次充值要够付一张常规账单，否则每次都要充两笔。"""
        cfg = _cfg()
        self.assertGreaterEqual(cfg.VCC_RECHARGE_AMOUNT, required_usd(cfg))

    def test_live_config_top_up_covers_live_need(self):
        """线上配置自身也要自洽：一次充值够付一单。

        这条盯的是真实配置而不是测试替身——备款额改了却忘了改充值额时，
        空卡会按整数倍充两笔，白白多锁一倍的钱。
        """
        from config import vcc
        self.assertGreaterEqual(vcc.VCC_RECHARGE_AMOUNT, required_usd(vcc))


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
        self.assertEqual(client.recharges[0][1], 35.0)

    def test_recharge_covers_shortfall_when_default_too_small(self):
        """默认充值额不够时按整数倍充，不能充完还是不足。"""
        client = FakeClient(card=0.0, account=500.0)
        cfg = _cfg(VCC_FUNDING_AMOUNT=5600.0, VCC_RECHARGE_AMOUNT=21.0)
        ensure_funds(client, "CARD", cfg)
        need = required_usd(cfg)          # 5600/56 + 1 = 101.0
        amount = client.recharges[0][1]
        self.assertGreaterEqual(amount, need)
        self.assertAlmostEqual(amount % 21.0, 0.0, places=2,
                               msg="应按配置额度的整数倍充值")

    def test_recharge_uses_configured_amount_not_odd_number(self):
        """卡里有零头时也要充整额，不能算出 34.22 这种数字。"""
        client = FakeClient(card=1.32, account=300.0)
        ensure_funds(client, "CARD", _cfg())
        self.assertEqual(client.recharges[0][1], 35.0)

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
        """扣了款但钱还在路上：提示别重发，不能静默继续。

        和幂等命中的区别在于账户真的被扣了、卡余额也开始变动，只是没到目标值。
        """
        client = FakeClient(card=1.0, account=300.0, settle=False)
        # 转账在途：卡上先到一部分，但不足以支付。
        client.wait_card_balance = lambda *a, **k: 10.0
        with self.assertRaises(InsufficientFunds) as ctx:
            ensure_funds(client, "CARD", _cfg())
        msg = str(ctx.exception)
        self.assertIn("未到账", msg)
        self.assertIn("不要重复发起", msg)
        self.assertEqual(len(client.recharges), 1)

    def test_idempotent_hit_is_reported_as_not_applied(self):
        """请求号被判重复单时，余额一点没动，必须和"转账在路上"区分开。

        两者处置完全不同：前者要去后台核对请求号，后者只能干等。
        """
        client = FakeClient(card=1.0, account=300.0)
        cfg = _cfg()
        # 请求号必须按实际会充的金额算，否则构造不出"同一单"的场景
        req = _request_no("CARD", cfg.VCC_RECHARGE_AMOUNT, attempt_key="1.00")
        client._idempotent_hits = {req}
        with self.assertRaises(InsufficientFunds) as ctx:
            ensure_funds(client, "CARD", cfg)
        msg = str(ctx.exception)
        self.assertIn("充值未生效", msg)
        self.assertIn("重复单", msg)
        self.assertNotIn("不要重复发起", msg)

    def test_rejected_status_raises(self):
        client = FakeClient(card=1.0, account=300.0, status="FAILED")
        with self.assertRaises(InsufficientFunds):
            ensure_funds(client, "CARD", _cfg())


class RequestNoIdempotencyTest(unittest.TestCase):
    """请求号必须接住真重试，又不能把两次合法充值算成同一单。

    2026-11-02 线上事故：09:00 充值 21.00 成功并花掉，11:56 余额不足再充，
    算出同一个请求号被 VCC 幂等拦截，返回成功却不划钱，卡余额卡在 2.76。
    """

    def test_same_intent_is_reproducible(self):
        # 真正的重试：余额没变，必须算出同一个号，让幂等接住。
        a = _request_no("CARD", 21.0, attempt_key="2.32")
        b = _request_no("CARD", 21.0, attempt_key="2.32")
        self.assertEqual(a, b)

    def test_second_topup_after_spending_differs(self):
        # 第一次充值前余额 2.32，花掉后再充时余额 2.76 —— 新的充值意图。
        first = _request_no("CARD", 21.0, attempt_key="2.32")
        second = _request_no("CARD", 21.0, attempt_key="2.76")
        self.assertNotEqual(first, second,
                            "同一天的第二次合法充值不能复用请求号，否则会被幂等吞掉")

    def test_different_cards_and_amounts_differ(self):
        base = _request_no("CARD", 21.0, attempt_key="2.32")
        self.assertNotEqual(base, _request_no("OTHER", 21.0, attempt_key="2.32"))
        self.assertNotEqual(base, _request_no("CARD", 42.0, attempt_key="2.32"))

    def test_two_sequential_topups_use_distinct_request_nos(self):
        """端到端：连充两次（中间花掉）必须产生两个不同请求号且都真的到账。"""
        cfg = _cfg()
        top = cfg.VCC_RECHARGE_AMOUNT
        client = FakeClient(card=2.32, account=300.0)
        ensure_funds(client, "CARD", cfg)
        self.assertAlmostEqual(client._card, 2.32 + top, places=2)

        client._card = 2.76  # 模拟支付花掉大部分
        ensure_funds(client, "CARD", cfg)
        self.assertAlmostEqual(client._card, 2.76 + top, places=2)

        self.assertEqual(len(client.seen_request_nos), 2)
        self.assertNotEqual(client.seen_request_nos[0], client.seen_request_nos[1])

    def test_prefix_kept_for_backend_lookup(self):
        self.assertTrue(_request_no("CARD", 21.0, attempt_key="x").startswith("autopay_"))


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
        self.assertEqual(r["recharge"], 35.0)

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
