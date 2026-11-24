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


SESSION_SRC = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")


class CustomCardNotVccManagedTest(unittest.TestCase):
    """自定义卡不在 VCC 名下，任何打到 VCC 的调用都会报"卡片不存在"。"""

    def test_funds_check_skipped_for_custom_card(self):
        idx = APP.index("funds_info = None")
        body = APP[idx:idx + 1800]
        self.assertIn("is_custom_id(", body)
        # 必须在 ensure_funds 之前就分流，否则照样打 VCC
        self.assertLess(body.index("is_custom_id("), body.index("ensure_funds("))

    def test_pick_card_skipped_for_custom_card(self):
        idx = SESSION_SRC.index("# 取卡要走白名单 IP")
        body = SESSION_SRC[idx:idx + 2600]
        self.assertIn("is_custom_id(want_card)", body)
        self.assertLess(body.index("is_custom_id(want_card)"),
                        body.index("client.pick_card("))

    def test_card_secrets_not_called_for_custom_card(self):
        self.assertIn("if not is_custom_id(card_id):", SESSION_SRC)
        idx = SESSION_SRC.index("if not is_custom_id(card_id):")
        self.assertIn("client.card_secrets", SESSION_SRC[idx:idx + 200])

    def test_3ds_polling_skipped_for_custom_card(self):
        """自定义卡没有验证码接口，轮询只会白等到超时。"""
        idx = SESSION_SRC.index("def _handle_3ds_interactive")
        body = SESSION_SRC[idx:idx + 1800]
        self.assertIn("is_custom_id(card_id)", body)
        self.assertLess(body.index("is_custom_id(card_id)"),
                        body.index("client.wait_3ds_code("))

    def test_funds_check_route_skips_custom_card(self):
        """弹窗选完卡就会调这个接口，不放行会弹"余额不足，无法发起支付"。"""
        idx = APP.index("def api_pay_funds_check")
        body = APP[idx:idx + 1600]
        self.assertIn("is_custom_id(card_id)", body)
        # 必须在 preflight 之前分流，否则照样打 VCC
        self.assertLess(body.index("is_custom_id(card_id)"),
                        body.index("preflight("))
        self.assertIn('"ok": True', body)


class ExpiryFormatTest(StoreTestBase):
    """卡池存 4 位年，Stripe 的有效期框是 MM / YY，必须转换。

    不转的话填入 "03 / 2030" 会被截成 "03 / 20"，回读校验失败，
    报 "填写 card_expiry 后校验不符"。
    """

    def test_store_keeps_four_digit_year(self):
        c = self.store.add_card("4242424242424242", "3", "2030", "123")
        sec = self.store.get_secrets(c["local_id"])
        self.assertEqual(sec["exp_month"], "03")
        self.assertEqual(sec["exp_year"], "2030")

    def test_two_digit_input_is_expanded(self):
        c = self.store.add_card("4242424242424242", "3", "30", "123")
        sec = self.store.get_secrets(c["local_id"])
        self.assertEqual(sec["exp_year"], "2030")

    def test_pay_session_converts_to_two_digit(self):
        """填表拿到的必须是两位年，否则 Stripe 框装不下。"""
        idx = SESSION_SRC.index("sec = get_secrets(lid)")
        body = SESSION_SRC[idx:idx + 1200]
        self.assertIn('[-2:]', body)
        self.assertIn('"exp_year": yy', body)

    def test_secrets_route_returns_two_digit_year(self):
        """VCC 分支返回两位年，自定义卡分支必须一致。"""
        idx = APP.index('def api_pay_card_secrets')
        body = APP[idx:idx + 2200]
        head = body[:body.index('"holder_name": sec["holder_name"]')]
        self.assertIn('[-2:]', head)

    def test_composed_expiry_digits_are_four(self):
        """端到端：拼出来的 "MM / YY" 只能有 4 位数字。"""
        c = self.store.add_card("4242424242424242", "7", "2032", "123")
        sec = self.store.get_secrets(c["local_id"])
        yy = "".join(ch for ch in sec["exp_year"] if ch.isdigit())[-2:]
        mm = "".join(ch for ch in sec["exp_month"] if ch.isdigit()).zfill(2)
        composed = f"{mm} / {yy}"
        digits = "".join(ch for ch in composed if ch.isdigit())
        self.assertEqual(composed, "07 / 32")
        self.assertEqual(len(digits), 4, f"{composed!r} 会被 Stripe 截断")


class CustomCardUiTest(unittest.TestCase):
    """自定义卡的 balance 是人工登记值，不能当实时余额用。"""

    def test_balance_gate_exempts_custom_card(self):
        """默认 balance=0，拿它和 need 比会让自有卡永远选不中。"""
        idx = HTML.index("const affordable =")
        body = HTML[max(0, idx - 900):idx + 400]
        self.assertIn("_isCustom", body)
        self.assertIn("_affords", body)

    def test_selected_card_check_uses_same_helper(self):
        """上次用的卡若是自有卡，也不能被判成"余额不足"。"""
        idx = HTML.index("const enough =")
        self.assertIn("_affords(c)", HTML[idx:idx + 120])

    def test_skipped_state_is_not_shown_as_sufficient(self):
        """查不到余额就别显示绿色"足够"，那是假信息。"""
        idx = HTML.index("async function _autoPayCheckFunds")
        body = HTML[idx:idx + 2200]
        self.assertIn("s.skipped", body)
        self.assertIn("无法查询余额", body)
        # 放行，但不是绿色的"足够"
        self.assertLess(body.index("s.skipped"), body.index("s.need_recharge"))

    def test_option_label_marks_custom_card(self):
        """0.00 USD 会被当成没钱，要标出这是自有卡。"""
        idx = HTML.index("sel.innerHTML = cards.map")
        body = HTML[idx:idx + 900]
        self.assertIn("自有卡", body)


class CustomCardPrepareTest(StoreTestBase):
    """真跑一遍准备阶段，确认一次 VCC 调用都没发生。"""

    @classmethod
    def setUpClass(cls):
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("需要 playwright，容器内可跑")

    class ExplodingVcc:
        """任何调用都炸，模拟"自定义卡在 VCC 查不到"。"""

        def __getattr__(self, name):
            def boom(*_a, **_kw):
                raise AssertionError(f"自定义卡不该调用 VCC.{name}")
            return boom

    def _cfg(self, card_id):
        class Cfg:
            VCC_CARD_ID = card_id
            VCC_MIN_CARD_BALANCE = 0.0
            VCC_NAME_SOURCE = "holder"
            VCC_BILLING_NAME = ""
            VCC_HOLDER_ID = 0
            VCC_BILLING_COUNTRY = "US"
            VCC_BILLING_STATE = "CA"
            VCC_BILLING_CITY = "Los Angeles"
            VCC_BILLING_LINE1 = "1 Main St"
            VCC_BILLING_LINE2 = ""
            VCC_BILLING_POSTAL = "90001"
            VCC_SCREENSHOT_DIR = ""
        return Cfg()

    def _run_prepare_only(self, ps, sess, cfg):
        """跑完准备阶段就停下，不开浏览器。

        CloakBrowser 返回 None 会走裸 Playwright 分支，而 sync_playwright()
        在测试线程里本身就会炸，所以直接让它抛一个可识别的错。
        """
        import core.vcc_pay_session as mod
        sentinel = RuntimeError("stop-before-browser")

        def boom(*_a, **_kw):
            raise sentinel

        orig_cloak = mod._try_cloak_browser
        mod._try_cloak_browser = boom
        try:
            ps._worker(sess, "https://example.test/pay", "", cfg,
                       self.ExplodingVcc())
        except RuntimeError as exc:
            if exc is not sentinel:
                raise
        finally:
            mod._try_cloak_browser = orig_cloak

    def test_prepare_uses_local_secrets_and_no_vcc_call(self):
        import core.vcc_pay_session as ps

        card = self.store.add_card("4242424242424242", "12", "28", "123",
                                   holder_name="Jane Doe", label="我的卡")
        cfg = self._cfg(card["card_id"])
        sess = ps.PaySession(sid="custom1", account_id=1, email="a@b.c")

        self._run_prepare_only(ps, sess, cfg)

        # 没有 AssertionError 说明一次 VCC 调用都没发生；
        # 掩码来自本地卡号，证明三要素取的是卡池而不是 VCC。
        self.assertEqual(sess.card_mask, "424242****4242")
        self.assertNotEqual(sess.state, ps.ST_FAILED)

    def test_missing_custom_card_fails_cleanly(self):
        import core.vcc_pay_session as ps

        cfg = self._cfg("local:99999")
        sess = ps.PaySession(sid="custom2", account_id=1, email="a@b.c")
        ps._worker(sess, "https://example.test/pay", "", cfg,
                   self.ExplodingVcc())
        self.assertEqual(sess.state, ps.ST_FAILED)
        self.assertIn("自定义卡不可用", sess.message)

    def test_holder_name_falls_back_to_config(self):
        """卡池没填姓名时用配置值，不能把空姓名填进表单。"""
        import core.vcc_pay_session as ps

        card = self.store.add_card("4242424242424242", "12", "28", "123",
                                   holder_name="", label="无名卡")
        cfg = self._cfg(card["card_id"])
        cfg.VCC_BILLING_NAME = "Fallback Name"
        sess = ps.PaySession(sid="custom3", account_id=1, email="a@b.c")

        self._run_prepare_only(ps, sess, cfg)

        # 走到了浏览器阶段，说明姓名校验通过、没停在准备阶段
        self.assertNotEqual(sess.state, ps.ST_FAILED)
        self.assertEqual(sess.card_mask, "424242****4242")

    def test_no_name_anywhere_fails_with_clear_message(self):
        import core.vcc_pay_session as ps

        card = self.store.add_card("4242424242424242", "12", "28", "123",
                                   holder_name="", label="无名卡")
        cfg = self._cfg(card["card_id"])
        sess = ps.PaySession(sid="custom4", account_id=1, email="a@b.c")
        ps._worker(sess, "https://example.test/pay", "", cfg,
                   self.ExplodingVcc())
        self.assertEqual(sess.state, ps.ST_FAILED)
        self.assertIn("持卡人姓名", sess.message)


class UiWiringTest(unittest.TestCase):
    def test_sidebar_entry(self):
        self.assertIn('data-tab="cards"', HTML)
        self.assertIn("银行卡池", HTML)

    def test_panel_and_loader(self):
        self.assertIn('id="tab-cards"', HTML)
        self.assertIn("tab === 'cards') loadCustomCards()", HTML)

    def test_tab_in_allowed_list(self):
        """不在 allowed 里的话 activateTab 会把它重置成 register，点了没反应。"""
        idx = HTML.index("const allowed = [")
        line = HTML[idx:HTML.index("]", idx)]
        self.assertIn("'cards'", line)

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
