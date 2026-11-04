# -*- coding: utf-8 -*-
"""卡片 ↔ 账单地址关联。

选卡后自动套用该卡最近一次用过的地址，下拉里"该卡用过"的地址单独分组。
不碰网络、不开浏览器、不动钱。
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class CardAddressLinkTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        import core.billing_address_store as store
        self.store = store
        self._orig = store.DB_PATH
        store.DB_PATH = str(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.store.DB_PATH = self._orig
        self._tmp.cleanup()

    A1 = {"name": "ONeal Di", "country": "US", "state": "AK",
          "city": "Anchorage", "line1": "750 West Dimond Boulevard",
          "line2": "", "postal": "99515"}
    A2 = {"name": "Jessica Martinez", "country": "US", "state": "CA",
          "city": "San Jose", "line1": "1 Infinite Loop",
          "line2": "", "postal": "95014"}

    def test_link_then_read_back(self):
        a = self.store.save_address(self.A1)
        self.store.link_card_address("CARD_A", a["id"])
        self.assertEqual(self.store.addresses_for_card("CARD_A"), [a["id"]])
        self.assertEqual(
            self.store.last_address_for_card("CARD_A")["line1"],
            "750 West Dimond Boulevard")

    def test_unknown_card_has_no_link(self):
        self.store.save_address(self.A1)
        self.assertEqual(self.store.addresses_for_card("NOPE"), [])
        self.assertIsNone(self.store.last_address_for_card("NOPE"))
        self.assertIsNone(self.store.last_address_for_card(""))

    def test_most_recent_address_wins(self):
        a = self.store.save_address(self.A1)
        b = self.store.save_address(self.A2)
        self.store.link_card_address("CARD_A", a["id"])
        self.store.link_card_address("CARD_A", b["id"])
        self.assertEqual(self.store.last_address_for_card("CARD_A")["id"], b["id"])
        # 再用回第一个，它就该排到前面
        self.store.link_card_address("CARD_A", a["id"])
        self.assertEqual(self.store.last_address_for_card("CARD_A")["id"], a["id"])
        self.assertEqual(set(self.store.addresses_for_card("CARD_A")),
                         {a["id"], b["id"]})

    def test_repeat_link_counts_not_duplicates(self):
        a = self.store.save_address(self.A1)
        for _ in range(3):
            self.store.link_card_address("CARD_A", a["id"])
        self.assertEqual(self.store.addresses_for_card("CARD_A"), [a["id"]])

    def test_cards_do_not_share_links(self):
        a = self.store.save_address(self.A1)
        b = self.store.save_address(self.A2)
        self.store.link_card_address("CARD_A", a["id"])
        self.store.link_card_address("CARD_B", b["id"])
        self.assertEqual(self.store.addresses_for_card("CARD_A"), [a["id"]])
        self.assertEqual(self.store.addresses_for_card("CARD_B"), [b["id"]])

    def test_deleting_address_drops_its_links(self):
        a = self.store.save_address(self.A1)
        self.store.link_card_address("CARD_A", a["id"])
        self.store.delete_address(a["id"])
        self.assertEqual(self.store.addresses_for_card("CARD_A"), [])
        self.assertEqual(self.store.card_links(), {})

    def test_blank_card_id_is_ignored(self):
        a = self.store.save_address(self.A1)
        self.store.link_card_address("", a["id"])
        self.store.link_card_address("   ", a["id"])
        self.assertEqual(self.store.card_links(), {})

    def test_card_links_groups_by_card(self):
        a = self.store.save_address(self.A1)
        b = self.store.save_address(self.A2)
        self.store.link_card_address("CARD_A", a["id"])
        self.store.link_card_address("CARD_A", b["id"])
        self.store.link_card_address("CARD_B", a["id"])
        links = self.store.card_links()
        self.assertEqual(set(links), {"CARD_A", "CARD_B"})
        self.assertEqual(links["CARD_A"][0], b["id"])   # 最近的排前面
        self.assertEqual(links["CARD_B"], [a["id"]])


class ApiShapeTest(unittest.TestCase):
    """接口要把关联一并带给前端，否则前端没法分组。"""

    def test_list_endpoint_returns_card_links(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        idx = src.index("def api_pay_billing_addresses()")
        body = src[idx:idx + 400]
        self.assertIn("card_links", body)

    def test_start_links_card_to_address(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        self.assertIn("link_card_address", src)
        # 关联必须发生在会话真正起来之后，和存地址同一段逻辑里
        link_pos = src.index("link_card_address(used_card")
        saved_pos = src.index("saved_address = None")
        self.assertGreater(link_pos, saved_pos)


class FrontendGroupingTest(unittest.TestCase):
    """下拉要分组，且换卡要触发套用。"""

    def setUp(self):
        self.html = (ROOT / "webui" / "templates"
                     / "index.html").read_text(encoding="utf-8")

    def test_renders_optgroups(self):
        self.assertIn("_autoPayRenderAddrOptions", self.html)
        self.assertIn("该卡用过", self.html)
        self.assertIn("其它地址", self.html)

    def test_card_change_applies_address(self):
        self.assertIn("_autoPayApplyCardAddress", self.html)
        idx = self.html.index("cardSel.onchange")
        window = self.html[idx:idx + 220]
        self.assertIn("_autoPayApplyCardAddress", window)
        self.assertIn("_autoPayCheckFunds", window)

    def test_links_are_fetched_into_cache(self):
        self.assertIn("_autoPayCardLinks", self.html)
        self.assertIn("s.card_links", self.html)

    def test_manual_address_change_clears_hint(self):
        idx = self.html.index("addrSel.onchange")
        window = self.html[idx:idx + 400]
        self.assertIn("autoPayAddrHint", window)

    def test_no_duplicate_option_ids_in_groups(self):
        """同一个地址不能既在"用过"又在"其它"里。"""
        idx = self.html.index("function _autoPayRenderAddrOptions")
        body = self.html[idx:idx + 1200]
        # 分组用的是互斥的 push，不是两次全量遍历
        self.assertIn("rank.has(String(a.id)) ? mine : others", body)
        self.assertEqual(len(re.findall(r"_autoPayAddrCache\.forEach", body)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
