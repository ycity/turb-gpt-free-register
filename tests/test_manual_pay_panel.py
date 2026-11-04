# -*- coding: utf-8 -*-
"""手动支付面板：给人长链 + 一键填表脚本。

服务端浏览器下 Stripe 会报 "unable to authenticate your payment method"，
同一条长链在人自己的浏览器里却能付成功。所以留一条人工通道：程序备好
长链、卡片三要素和账单地址，人粘脚本填表、自己点提交，付完回来登记。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")


class ModalExistsTest(unittest.TestCase):
    def test_entry_button_in_pay_modal(self):
        self.assertIn('id="btnAutoPayManual"', HTML)

    def test_modal_markup(self):
        for el in ("manualPayModal", "manualPayUrl", "manualPayCard",
                   "manualPayAddr", "manualPayScript", "manualPayPromo"):
            self.assertIn(f'id="{el}"', HTML, f"缺少 {el}")

    def test_script_box_is_readonly(self):
        idx = HTML.index('id="manualPayScript"')
        self.assertIn("readonly", HTML[idx:idx + 200])

    def test_has_copy_buttons(self):
        self.assertIn('id="btnManualPayCopyUrl"', HTML)
        self.assertIn('id="btnManualPayCopyScript"', HTML)


class ScriptGeneratorTest(unittest.TestCase):
    """生成的脚本必须覆盖所有必填字段，且不自动提交。"""

    def setUp(self):
        start = HTML.index("function _manualPayScriptText")
        self.body = HTML[start:HTML.index("async function _manualPayRender")]

    def test_covers_every_required_selector(self):
        for sel in ("#cardNumber", "#cardExpiry", "#cardCvc", "#billingName",
                    "select#billingCountry", "#billingAddressLine1",
                    "#billingLocality", "#billingPostalCode",
                    "select#billingAdministrativeArea",
                    "#termsOfServiceConsentCheckbox"):
            self.assertIn(sel, self.body, f"脚本漏了 {sel}")

    def test_never_clicks_submit(self):
        """提交必须由人决定，脚本只填表。"""
        self.assertNotIn("hosted-payment-submit-button", self.body)
        self.assertNotIn(".submit()", self.body)

    def test_unlocks_disabled_fields(self):
        self.assertIn("el.disabled = false", self.body)
        self.assertIn("el.readOnly = false", self.body)

    def test_restores_lock_state(self):
        self.assertIn("el.disabled = d", self.body)
        self.assertIn("el.readOnly = r", self.body)

    def test_country_set_before_state(self):
        """省份下拉按国家重建，顺序反了选不到州。"""
        self.assertLess(self.body.index("billingCountry"),
                        self.body.index("billingAdministrativeArea"))

    def test_state_filled_after_delay(self):
        self.assertIn("setTimeout", self.body)

    def test_values_are_json_escaped(self):
        """地址里可能有引号，必须走 JSON.stringify 而不是裸拼。"""
        self.assertIn("JSON.stringify", self.body)


class ManualDoneEndpointTest(unittest.TestCase):
    def test_route_exists(self):
        self.assertIn('@app.post("/api/pay/manual-done")', APP)

    def test_records_promo(self):
        idx = APP.index("def api_pay_manual_done")
        body = APP[idx:idx + 2600]
        self.assertIn("record_used_promo_code", body)

    def test_links_card_and_address(self):
        idx = APP.index("def api_pay_manual_done")
        body = APP[idx:idx + 2600]
        self.assertIn("link_card_address", body)
        self.assertIn("pay_last_card_id", body)

    def test_triggers_plan_check(self):
        idx = APP.index("def api_pay_manual_done")
        body = APP[idx:idx + 2600]
        self.assertIn("enqueue_account_plan_check", body)

    def test_requires_account_id(self):
        idx = APP.index("def api_pay_manual_done")
        body = APP[idx:idx + 2600]
        self.assertIn("缺少 account_id", body)

    def test_partial_failures_do_not_abort(self):
        """优惠码登记失败不该连累套餐查询。"""
        idx = APP.index("def api_pay_manual_done")
        body = APP[idx:idx + 2600]
        self.assertGreaterEqual(body.count("except"), 3)


class CardSecretsEndpointTest(unittest.TestCase):
    def test_route_exists(self):
        self.assertIn('@app.get("/api/pay/card-secrets")', APP)

    def test_requires_card_id(self):
        idx = APP.index("def api_pay_card_secrets")
        body = APP[idx:idx + 1400]
        self.assertIn("缺少 card_id", body)

    def test_returns_three_elements(self):
        idx = APP.index("def api_pay_card_secrets")
        body = APP[idx:idx + 1400]
        for k in ("number", "exp_month", "exp_year", "cvc"):
            self.assertIn(f'"{k}"', body)

    def test_does_not_log_the_plaintext(self):
        """卡号明文不能进日志。"""
        idx = APP.index("def api_pay_card_secrets")
        body = APP[idx:idx + 1400]
        for line in body.splitlines():
            if "logger." in line:
                self.assertNotIn("sec", line)
                self.assertNotIn("number", line)


class UsesRealGlobalsTest(unittest.TestCase):
    """别引用不存在的变量和接口。"""

    def setUp(self):
        start = HTML.index("async function _manualPayOpen")
        self.body = HTML[start:HTML.index("async function _manualPayDone")]

    def test_uses_ACCOUNTS_not_invented_cache(self):
        self.assertIn("ACCOUNTS", self.body)
        self.assertNotIn("_accCache", self.body)

    def test_no_call_to_nonexistent_account_endpoint(self):
        self.assertNotIn("/api/accounts/${", self.body)

    def test_reuses_loaded_card_and_address_caches(self):
        self.assertIn("_autoPayCardCache", self.body)
        self.assertIn("_autoPayAddrCache", self.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
