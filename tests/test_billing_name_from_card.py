# -*- coding: utf-8 -*-
"""账单姓名优先取卡自带的 holderName。

线上遇到的问题：卡 537872****5290 的 holderId=1，但 cardholders/query
只返回 7 个持卡人（1671~1976），不含 ID 1，于是 pick_cardholder 抛
"未找到 holderId=1 的持卡人"。而那张卡本身就带着 holderName。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class StaleHolderVcc:
    """复刻线上状态：卡带姓名，持卡人表里查不到它的 holderId。"""

    def __init__(self):
        self.holder_calls = 0

    def pick_card(self, **_kw):
        return {"cardId": "TEST", "cardNoMask": "537872****5290",
                "holderId": 1, "holderName": "Jessica Martinez"}

    def pick_cardholder(self, hid):
        self.holder_calls += 1
        from core.vcc_client import VccError
        raise VccError(f"未找到 holderId={hid} 的持卡人")

    def card_secrets(self, _cid):
        return {"number": "5378727100005290", "exp_month": "10",
                "exp_year": "28", "cvc": "075"}

    def wait_3ds_code(self, *_a, **_kw):
        return ""


class NamelessCardVcc(StaleHolderVcc):
    """卡没带姓名时必须回退到持卡人接口。"""

    def pick_card(self, **_kw):
        return {"cardId": "TEST", "cardNoMask": "537872****5290",
                "holderId": 1838}

    def pick_cardholder(self, hid):
        self.holder_calls += 1
        return {"holderName": "ONeal Di"}


def _cfg(**over):
    from config import vcc as base

    class Cfg:
        pass

    for name in dir(base):
        if name.startswith("VCC_"):
            setattr(Cfg, name, getattr(base, name))
    Cfg.VCC_SESSION_TIMEOUT = 3.0
    Cfg.VCC_3DS_ENABLED = False
    for key, value in over.items():
        setattr(Cfg, key, value)
    return Cfg()


def _resolve_name(client, cfg, card_row):
    """复刻两处支付代码里的姓名解析分支。"""
    from core.vcc_checkout_pay import VccPayError
    if (cfg.VCC_NAME_SOURCE or "holder").lower() == "config":
        name = (cfg.VCC_BILLING_NAME or "").strip()
        if not name:
            raise VccPayError("VCC_NAME_SOURCE=config 但 VCC_BILLING_NAME 为空")
        return name
    name = str(card_row.get("holderName") or "").strip()
    if not name:
        holder = client.pick_cardholder(int(cfg.VCC_HOLDER_ID or 0))
        name = str(holder.get("holderName") or "").strip()
    if not name:
        raise VccPayError("持卡人姓名为空，请改用 VCC_BILLING_NAME")
    return name


class TestBillingNameFromCard(unittest.TestCase):
    def test_card_holder_name_wins_without_querying_holders(self):
        client = StaleHolderVcc()
        name = _resolve_name(client, _cfg(VCC_HOLDER_ID=1),
                             client.pick_card())
        self.assertEqual(name, "Jessica Martinez")
        self.assertEqual(client.holder_calls, 0,
                         "卡已带姓名时不应再查持卡人表")

    def test_falls_back_to_holder_query_when_card_has_no_name(self):
        client = NamelessCardVcc()
        name = _resolve_name(client, _cfg(VCC_HOLDER_ID=1838),
                             client.pick_card())
        self.assertEqual(name, "ONeal Di")
        self.assertEqual(client.holder_calls, 1)

    def test_config_source_still_overrides_card(self):
        client = StaleHolderVcc()
        name = _resolve_name(
            client,
            _cfg(VCC_NAME_SOURCE="config", VCC_BILLING_NAME="Manual Name"),
            client.pick_card())
        self.assertEqual(name, "Manual Name")
        self.assertEqual(client.holder_calls, 0)


class TestRealPaySessionPath(unittest.TestCase):
    """直接验证 vcc_pay_session 源码里确实是这个顺序。"""

    def test_pay_session_reads_card_row_first(self):
        src = (Path(__file__).resolve().parents[1]
               / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        idx = src.index('raise VccPayError("持卡人姓名为空')
        window = src[max(0, idx - 700):idx]
        card_pos = window.rindex('card_row.get("holderName")')
        holder_pos = window.rindex("pick_cardholder")
        self.assertLess(card_pos, holder_pos,
                        "必须先读 card_row.holderName，再回退 pick_cardholder")

    def test_checkout_pay_reads_card_row_first(self):
        src = (Path(__file__).resolve().parents[1]
               / "core" / "vcc_checkout_pay.py").read_text(encoding="utf-8")
        idx = src.index('raise VccPayError("持卡人姓名为空')
        window = src[max(0, idx - 700):idx]
        card_pos = window.rindex('card_row.get("holderName")')
        holder_pos = window.rindex("pick_cardholder")
        self.assertLess(card_pos, holder_pos,
                        "必须先读 card_row.holderName，再回退 pick_cardholder")


if __name__ == "__main__":
    unittest.main(verbosity=2)
