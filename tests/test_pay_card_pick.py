# -*- coding: utf-8 -*-
"""选卡默认值的优先级。

实际踩到的问题：配置里写死的 VCC_CARD_ID 指向一张余额只剩 3.2 的卡，
而账户下另有一张 21.57 的。每次打开弹窗都默认选中那张付不起的，
还得手动换。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HTML = (Path(__file__).resolve().parents[1]
        / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
APP = (Path(__file__).resolve().parents[1]
       / "webui" / "app.py").read_text(encoding="utf-8")


def pick(cards, preferred, need):
    """复刻前端选卡规则，便于断言优先级。"""
    by_id = {c["card_id"]: c for c in cards}

    def pickable(cid):
        c = by_id.get(cid)
        return c if (c and c.get("usable")) else None

    affordable = next(
        (c for c in cards
         if c.get("usable") and c["balance"] >= need and pickable(c["card_id"])),
        None)

    if pickable(preferred):
        c = by_id[preferred]
        if not need or c["balance"] >= need:
            return preferred, "用首选"
        if affordable:
            return affordable["card_id"], "首选余额不足，改选够付的"
        return preferred, "都不够，保留首选并提示充值"
    if affordable:
        return affordable["card_id"], "首选不可用，自动选够付的"
    return "", "无可用卡"


RICH = {"card_id": "rich", "balance": 21.57, "usable": True, "mask": "****4157"}
POOR = {"card_id": "poor", "balance": 3.20, "usable": True, "mask": "****8361"}
DEAD = {"card_id": "dead", "balance": 99.0, "usable": False, "mask": "****0000"}
NEED = 21.54


class PickPriorityTest(unittest.TestCase):
    def test_preferred_with_enough_balance_wins(self):
        cid, _ = pick([RICH, POOR], "rich", NEED)
        self.assertEqual(cid, "rich")

    def test_preferred_too_poor_switches_to_affordable(self):
        """线上就是这个场景：配置指向余额不足的卡。"""
        cid, why = pick([RICH, POOR], "poor", NEED)
        self.assertEqual(cid, "rich")
        self.assertIn("余额不足", why)

    def test_no_preference_picks_affordable(self):
        cid, _ = pick([RICH, POOR], "", NEED)
        self.assertEqual(cid, "rich")

    def test_unusable_card_never_chosen(self):
        """冻结的卡余额再多也不能选；此时没有够付的卡，宁可不选。"""
        cid, _ = pick([DEAD, POOR], "dead", NEED)
        self.assertNotEqual(cid, "dead")
        self.assertEqual(cid, "", "没有可用且够付的卡时不该硬选一张")

    def test_unusable_preferred_falls_back_to_affordable(self):
        cid, why = pick([DEAD, RICH, POOR], "dead", NEED)
        self.assertEqual(cid, "rich")
        self.assertIn("首选不可用", why)

    def test_all_poor_keeps_preferred_and_warns(self):
        cid, why = pick([POOR], "poor", NEED)
        self.assertEqual(cid, "poor")
        self.assertIn("都不够", why)

    def test_no_need_info_falls_back_to_preferred(self):
        """拿不到汇率时 need=0，不应因此否决首选。"""
        cid, _ = pick([RICH, POOR], "poor", 0)
        self.assertEqual(cid, "poor")

    def test_empty_pool(self):
        cid, _ = pick([], "x", NEED)
        self.assertEqual(cid, "")


class WiringTest(unittest.TestCase):
    def test_backend_exposes_usd_rate(self):
        seg = APP[APP.index("def api_pay_defaults"):]
        seg = seg[:seg.index('@app.get("/api/pay/active")')]
        self.assertIn('"usd_rate"', seg, "前端要靠它估算需要多少 USD")
        self.assertIn('"last_card_id"', seg)

    def test_frontend_caches_cards(self):
        self.assertIn("_autoPayCardCache", HTML)

    def test_frontend_has_affordable_fallback(self):
        seg = HTML[HTML.index("// 选卡优先级"):]
        seg = seg[:seg.index("await _autoPayCheckFunds")]
        self.assertIn("affordable", seg)
        self.assertIn("余额不足", seg)

    def test_records_card_after_session_starts(self):
        start = APP.index("start_session(account_id=")
        record = APP.index('db.set_meta("pay_last_card_id"')
        self.assertLess(start, record, "会话没起来就记，会把失败的选择记成偏好")


if __name__ == "__main__":
    unittest.main()
