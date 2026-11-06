# -*- coding: utf-8 -*-
"""自定义银行卡池。

VCC 接口之外手工登记的卡，支付时和 VCC 卡一起出现在选卡列表里。
卡 id 统一加 "local:" 前缀，避免和 VCC 的 cardId 撞上。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        import core.custom_card_store as store
        self.store = store
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(path)
        self._old = store.DB_PATH
        store.DB_PATH = path
        self._path = path

    def tearDown(self):
        self.store.DB_PATH = self._old
        if os.path.exists(self._path):
            os.unlink(self._path)


class AddCardTest(StoreTestBase):
    def test_add_and_list(self):
        c = self.store.add_card("4242424242424242", "12", "28", "123",
                                label="测试卡", balance=25.5)
        self.assertEqual(c["mask"], "424242****4242")
        self.assertEqual(c["source"], "custom")
        self.assertTrue(c["usable"])
        self.assertEqual(len(self.store.list_cards()), 1)

    def test_card_id_is_prefixed(self):
        """不能和 VCC 的 cardId 撞上。"""
        c = self.store.add_card("4242424242424242", "12", "28", "123")
        self.assertTrue(c["card_id"].startswith("local:"))
        self.assertTrue(self.store.is_custom_id(c["card_id"]))
        self.assertEqual(self.store.local_id_of(c["card_id"]), c["local_id"])

    def test_vcc_id_not_mistaken_for_custom(self):
        self.assertFalse(self.store.is_custom_id("c_123abc"))
        self.assertEqual(self.store.local_id_of("c_123abc"), 0)

    def test_luhn_rejects_typo(self):
        """错一位就提交的话，Stripe 只会给个笼统的拒绝，不如本地拦住。"""
        with self.assertRaises(self.store.CustomCardError):
            self.store.add_card("1234567890123456", "12", "28", "123")

    def test_duplicate_rejected(self):
        self.store.add_card("4242424242424242", "12", "28", "123")
        with self.assertRaises(self.store.CustomCardError):
            self.store.add_card("4242 4242 4242 4242", "01", "30", "456")

    def test_two_digit_year_expanded(self):
        c = self.store.add_card("4242424242424242", "3", "28", "123")
        self.assertEqual(c["exp_month"], "03")
        self.assertEqual(c["exp_year"], "2028")

    def test_bad_month_rejected(self):
        for m in ("0", "13", "99"):
            with self.assertRaises(self.store.CustomCardError, msg=m):
                self.store.add_card("4242424242424242", m, "28", "123")

    def test_cvv_length_checked(self):
        for code in ("12", "12345", ""):
            with self.assertRaises(self.store.CustomCardError, msg=code):
                self.store.add_card("4242424242424242", "12", "28", code)

    def test_separators_tolerated(self):
        c = self.store.add_card("4242-4242 4242.4242", "12", "28", "123")
        self.assertEqual(self.store.get_secrets(c["local_id"])["card_number"],
                         "4242424242424242")


class UpdateDeleteTest(StoreTestBase):
    def setUp(self):
        super().setUp()
        self.card = self.store.add_card("4242424242424242", "12", "28", "123",
                                        label="原名", balance=10)

    def test_update_fields(self):
        c = self.store.update_card(self.card["local_id"],
                                   label="新名", balance=99)
        self.assertEqual(c["label"], "新名")
        self.assertEqual(c["balance"], 99.0)

    def test_card_number_not_editable(self):
        """卡号是唯一键，改了就是另一张卡。"""
        with self.assertRaises(self.store.CustomCardError):
            self.store.update_card(self.card["local_id"], card_no="4000...")

    def test_toggle_enabled(self):
        c = self.store.update_card(self.card["local_id"], enabled=False)
        self.assertFalse(c["usable"])
        self.assertEqual(c["status"], "DISABLED")
        self.assertEqual(len(self.store.list_cards(only_enabled=True)), 0)
        self.assertEqual(len(self.store.list_cards()), 1)

    def test_update_missing_card(self):
        with self.assertRaises(self.store.CustomCardError):
            self.store.update_card(9999, label="x")

    def test_delete(self):
        self.assertTrue(self.store.delete_card(self.card["local_id"]))
        self.assertEqual(len(self.store.list_cards()), 0)
        self.assertFalse(self.store.delete_card(self.card["local_id"]))


class SecretsTest(StoreTestBase):
    def test_secrets_returned_plain(self):
        c = self.store.add_card("4242424242424242", "12", "28", "123",
                                holder_name="Jane Doe")
        s = self.store.get_secrets(c["local_id"])
        self.assertEqual(s["card_number"], "4242424242424242")
        self.assertEqual(s["exp_year"], "2028")
        self.assertEqual(s["cvv"], "123")
        self.assertEqual(s["holder_name"], "Jane Doe")

    def test_list_does_not_leak_cvv(self):
        """列表接口只给掩码，别把 CVV 带到前端。"""
        self.store.add_card("4242424242424242", "12", "28", "123")
        row = self.store.list_cards()[0]
        self.assertNotIn("cvv", row)
        self.assertNotIn("card_no", row)

    def test_mark_used_counts(self):
        c = self.store.add_card("4242424242424242", "12", "28", "123")
        self.store.mark_used(c["local_id"])
        self.assertEqual(self.store.list_cards()[0]["use_count"], 1)


class ApiWiringTest(unittest.TestCase):
    def test_crud_endpoints_registered(self):
        for route in ('@app.get("/api/custom-cards")',
                      '@app.post("/api/custom-cards")',
                      '@app.post("/api/custom-cards/<int:local_id>")',
                      '@app.post("/api/custom-cards/<int:local_id>/delete")',
                      '@app.get("/api/custom-cards/<int:local_id>/secrets")'):
            self.assertIn(route, APP)

    def test_pay_cards_merges_custom(self):
        idx = APP.index('def api_pay_cards')
        body = APP[idx:idx + 2600]
        self.assertIn("list_cards as list_custom", body)
        self.assertIn("only_enabled=True", body)

    def test_disabled_cards_hidden_from_pay(self):
        """停用的卡不该出现在支付选卡列表里。"""
        idx = APP.index('def api_pay_cards')
        body = APP[idx:idx + 2600]
        self.assertIn("only_enabled=True", body)

    def test_vcc_failure_still_returns_custom(self):
        """VCC 挂了也要能用自己的卡，否则手头有卡却付不了。"""
        idx = APP.index('def api_pay_cards')
        body = APP[idx:idx + 2600]
        self.assertIn("vcc_error", body)

    def test_secrets_route_handles_custom_id(self):
        idx = APP.index('def api_pay_card_secrets')
        body = APP[idx:idx + 1800]
        self.assertIn("is_custom_id(card_id)", body)
        self.assertIn("mark_used", body)


class UiWiringTest(unittest.TestCase):
    def test_sidebar_entry(self):
        self.assertIn('data-tab="cards"', HTML)
        self.assertIn("银行卡池", HTML)

    def test_panel_and_loader(self):
        self.assertIn('id="tab-cards"', HTML)
        self.assertIn("tab === 'cards') loadCustomCards()", HTML)

    def test_modal_in_global_selector(self):
        """不进这个列表的话弹窗没有 position:fixed，会垫在页面流里。"""
        self.assertIn("#cardEditModal:not(.hidden)", HTML)

    def test_modal_in_scroll_lock(self):
        self.assertIn("$('#cardEditModal').classList.contains('hidden')", HTML)

    def test_card_number_locked_when_editing(self):
        idx = HTML.index("function openCardEdit")
        body = HTML[idx:idx + 1500]
        self.assertIn("noInput.disabled = !!c", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
