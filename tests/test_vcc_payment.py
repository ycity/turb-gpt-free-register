# -*- coding: utf-8 -*-
"""VCC 支付纯逻辑单测：签名、解密、金额解析、资金闸门。

不碰网络、不开浏览器、不动钱。
"""
import base64
import hashlib
import hmac
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.vcc_checkout_pay import (  # noqa: E402
    AmountGateError, check_amount_gate, parse_amount,
)
from core.vcc_client import (  # noqa: E402
    SENSITIVE_KEY_INFO, VccClient, VccError, _normalize_expiry,
    decrypt_sensitive, load_credentials_file,
)


def _encrypt(plain: str, webhook_secret: str) -> str:
    """按文档口径造一个密文，用于验证解密实现。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = hmac.new(webhook_secret.encode(), SENSITIVE_KEY_INFO, hashlib.sha256).digest()
    iv = b"\x01" * 12
    blob = iv + AESGCM(key).encrypt(iv, plain.encode(), None)
    return base64.urlsafe_b64encode(blob).decode().rstrip("=")


class SigningTest(unittest.TestCase):
    def test_signature_matches_documented_prehash(self):
        client = VccClient("ak_x", "sk_y")
        body = b'{"pageNum":1}'
        sig = client._sign("POST", "/openapi/v1/cards/query", "1700000000000", "abc", body)
        expected_prehash = "\n".join([
            "POST", "/openapi/v1/cards/query", "1700000000000", "abc",
            hashlib.sha256(body).hexdigest(),
        ])
        self.assertEqual(sig, hmac.new(b"sk_y", expected_prehash.encode(),
                                       hashlib.sha256).hexdigest())

    def test_body_hash_is_of_raw_body_not_reserialized(self):
        """签名必须用实际发出的原始 body，否则服务端算不出同一个 hash。"""
        client = VccClient("ak_x", "sk_y")
        a = client._sign("POST", "/p", "1", "n", b'{"a":1,"b":2}')
        b = client._sign("POST", "/p", "1", "n", b'{"b":2,"a":1}')
        self.assertNotEqual(a, b)

    def test_missing_credentials_rejected(self):
        with self.assertRaises(VccError):
            VccClient("", "sk")
        with self.assertRaises(VccError):
            VccClient("ak", "")


class DecryptTest(unittest.TestCase):
    def test_roundtrip(self):
        secret = "whsec_test_value"
        self.assertEqual(decrypt_sensitive(_encrypt("4860123412341234", secret), secret),
                         "4860123412341234")

    def test_unpadded_base64url_accepted(self):
        secret = "whsec_test_value"
        token = _encrypt("123", secret)
        self.assertNotIn("=", token)
        self.assertEqual(decrypt_sensitive(token, secret), "123")

    def test_wrong_secret_fails_closed(self):
        token = _encrypt("4860123412341234", "whsec_right")
        with self.assertRaises(VccError):
            decrypt_sensitive(token, "whsec_wrong")

    def test_api_secret_is_not_the_decryption_key(self):
        """文档明确：解密用 Webhook Secret，不是 API Secret。"""
        token = _encrypt("4860123412341234", "whsec_right")
        with self.assertRaises(VccError):
            decrypt_sensitive(token, "sk_api_secret")

    def test_missing_webhook_secret_reports_clearly(self):
        with self.assertRaises(VccError) as ctx:
            decrypt_sensitive("abc", "")
        self.assertIn("Webhook Secret", str(ctx.exception))

    def test_empty_ciphertext_returns_empty(self):
        self.assertEqual(decrypt_sensitive("", "whsec"), "")


class ExpiryTest(unittest.TestCase):
    def test_common_formats(self):
        for raw, want in (
            ("12/28", ("12", "28")),
            ("12/2028", ("12", "28")),
            ("1228", ("12", "28")),
            ("122028", ("12", "28")),
            ("2028-12", ("12", "28")),
        ):
            self.assertEqual(_normalize_expiry(raw), want, raw)

    def test_blank(self):
        self.assertEqual(_normalize_expiry(""), ("", ""))


class CredFileTest(unittest.TestCase):
    def test_colon_and_equals_both_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.txt"
            path.write_text("key: ak_1\nsecret = sk_2\n# note\nwebhook_secret: whsec_3\n",
                            encoding="utf-8")
            creds = load_credentials_file(path)
        self.assertEqual(creds["key"], "ak_1")
        self.assertEqual(creds["secret"], "sk_2")
        self.assertEqual(creds["webhook_secret"], "whsec_3")
        self.assertNotIn("# note", creds)


class ParseAmountTest(unittest.TestCase):
    def test_captured_page_values(self):
        """实采 HTML 里的两个金额。"""
        self.assertEqual(parse_amount("EGP\u00a01,311.00"), ("EGP", 1311.00))
        self.assertEqual(parse_amount("EGP\u00a01,150.00"), ("EGP", 1150.00))

    def test_negative_discount_line(self):
        self.assertEqual(parse_amount("-EGP\u00a01,150.00"), ("EGP", -1150.00))

    def test_european_decimal_comma(self):
        self.assertEqual(parse_amount("EUR 1.150,00"), ("EUR", 1150.00))
        self.assertEqual(parse_amount("EUR 1150,00"), ("EUR", 1150.00))

    def test_no_decimals(self):
        self.assertEqual(parse_amount("JPY 1,150"), ("JPY", 1150.0))

    def test_symbol_prefixed(self):
        currency, amount = parse_amount("US$1,150.00")
        self.assertEqual(amount, 1150.00)
        self.assertEqual(currency, "USD")

    def test_unparseable(self):
        self.assertEqual(parse_amount(""), ("", None))
        self.assertEqual(parse_amount("—"), ("", None))


class AmountGateTest(unittest.TestCase):
    """闸门语义：只有精确等于期望值才放行。"""

    GATE = dict(expected=1150.0, expected_currency="EGP",
                tolerance=0.01, max_charge=1200.0)

    def test_expected_amount_passes(self):
        check_amount_gate(1150.00, "EGP", **self.GATE)

    def test_within_tolerance_passes(self):
        check_amount_gate(1150.01, "EGP", **self.GATE)

    def test_the_taxed_total_is_blocked(self):
        """EG 下的 1311 必须被拦住——这正是用户要的闸门。"""
        with self.assertRaises(AmountGateError) as ctx:
            check_amount_gate(1311.00, "EGP", **self.GATE)
        self.assertIn("超过上限", str(ctx.exception))

    def test_amount_above_expected_but_under_cap_still_blocked(self):
        with self.assertRaises(AmountGateError) as ctx:
            check_amount_gate(1160.00, "EGP", **self.GATE)
        self.assertIn("不等于期望", str(ctx.exception))

    def test_full_price_blocked_by_cap(self):
        """优惠码没生效 → 2300，必须拦住。"""
        with self.assertRaises(AmountGateError):
            check_amount_gate(2300.00, "EGP", **self.GATE)

    def test_currency_mismatch_blocked(self):
        with self.assertRaises(AmountGateError) as ctx:
            check_amount_gate(1150.00, "USD", **self.GATE)
        self.assertIn("币种", str(ctx.exception))

    def test_unparsed_amount_fails_closed(self):
        with self.assertRaises(AmountGateError):
            check_amount_gate(None, "EGP", **self.GATE)

    def test_unknown_currency_fails_closed(self):
        """读不到币种时不能放行：同样的数字换个币种金额完全不同。"""
        with self.assertRaises(AmountGateError) as ctx:
            check_amount_gate(1150.00, "", **self.GATE)
        self.assertIn("无法识别页面币种", str(ctx.exception))

    def test_unknown_currency_allowed_only_when_check_disabled(self):
        check_amount_gate(1150.00, "", expected=1150.0, expected_currency="",
                          tolerance=0.01, max_charge=1200.0)

    def test_cap_applies_even_when_equality_check_disabled(self):
        with self.assertRaises(AmountGateError):
            check_amount_gate(1500.0, "EGP", expected=0.0, expected_currency="",
                              tolerance=0.01, max_charge=1200.0)

    def test_equality_disabled_allows_any_amount_under_cap(self):
        check_amount_gate(999.0, "EGP", expected=0.0, expected_currency="",
                          tolerance=0.01, max_charge=1200.0)

    def test_lower_than_expected_also_blocked(self):
        """异常偏低同样可疑，不放行。"""
        with self.assertRaises(AmountGateError):
            check_amount_gate(1.00, "EGP", **self.GATE)


class ConfigDefaultsTest(unittest.TestCase):
    def test_gate_defaults_are_consistent(self):
        from config import vcc
        self.assertEqual(vcc.VCC_EXPECTED_AMOUNT, 1150.0)
        # 上限必须高于期望值，否则闸门自相矛盾、永远不过
        self.assertGreater(vcc.VCC_MAX_CHARGE, vcc.VCC_EXPECTED_AMOUNT)
        # 默认不启用，避免误扣
        self.assertFalse(vcc.VCC_AUTO_PAY_ENABLED)

    def test_billing_address_is_complete(self):
        """账单地址随卡变化，这里只校验必填项齐全、州与国家匹配。"""
        from config import vcc
        for name in ("VCC_BILLING_COUNTRY", "VCC_BILLING_STATE",
                     "VCC_BILLING_CITY", "VCC_BILLING_LINE1",
                     "VCC_BILLING_POSTAL"):
            self.assertTrue(getattr(vcc, name), f"{name} 不能为空")
        if vcc.VCC_BILLING_COUNTRY == "US":
            self.assertRegex(vcc.VCC_BILLING_STATE, r"^[A-Z]{2}$")
            self.assertRegex(vcc.VCC_BILLING_POSTAL, r"^\d{5}(-\d{4})?$")


class PickCardTest(unittest.TestCase):
    """选卡。实测接口返回的 cardStatus 是字符串 'ACTIVE'，余额字段叫 balance。"""

    @staticmethod
    def _client(cards, balances=None):
        cl = VccClient("k", "s", webhook_secret="w")
        cl.list_cards = lambda **_kw: list(cards)
        cl.card_balance = lambda cid: (balances or {}).get(cid, {})
        return cl

    def test_accepts_string_status(self):
        card = {"cardId": "a", "cardStatus": "ACTIVE", "balance": "21.00"}
        self.assertEqual(self._client([card]).pick_card()["cardId"], "a")

    def test_accepts_legacy_numeric_status(self):
        card = {"cardId": "a", "cardStatus": 1, "balance": "21.00"}
        self.assertEqual(self._client([card]).pick_card()["cardId"], "a")

    def test_rejects_inactive_string_status(self):
        card = {"cardId": "a", "cardStatus": "FROZEN", "balance": "21.00"}
        with self.assertRaises(VccError):
            self._client([card]).pick_card(card_id="a")

    def test_min_balance_reads_balance_field(self):
        """余额筛选必须认 balance 字段，否则所有卡都会被当成 0 余额。"""
        cards = [{"cardId": "poor", "cardStatus": "ACTIVE", "balance": "0.46"},
                 {"cardId": "rich", "cardStatus": "ACTIVE", "balance": "21.00"}]
        picked = self._client(cards).pick_card(min_balance=10.0)
        self.assertEqual(picked["cardId"], "rich")

    def test_min_balance_rejects_when_all_too_low(self):
        cards = [{"cardId": "a", "cardStatus": "ACTIVE", "balance": "0.46"}]
        with self.assertRaises(VccError):
            self._client(cards).pick_card(min_balance=10.0)


class BuildProxyOptionTest(unittest.TestCase):
    """Chromium 不认 server 里内嵌的 user:pass，必须拆字段。

    不拆的后果不是认证失败，而是所有请求静默卡到超时，很难排查。
    """

    @staticmethod
    def _build(raw):
        from core.vcc_checkout_pay import build_proxy_option
        return build_proxy_option(raw)

    def test_credentials_are_split_out_of_server(self):
        opt = self._build("http://yun:pw123@1.2.3.4:48901")
        self.assertEqual(opt["server"], "http://1.2.3.4:48901")
        self.assertEqual(opt["username"], "yun")
        self.assertEqual(opt["password"], "pw123")
        self.assertNotIn("@", opt["server"])

    def test_plain_proxy_has_no_credential_keys(self):
        opt = self._build("http://1.2.3.4:8080")
        self.assertEqual(opt["server"], "http://1.2.3.4:8080")
        self.assertNotIn("username", opt)

    def test_bare_hostport_defaults_to_http(self):
        self.assertEqual(self._build("1.2.3.4:8080")["server"],
                         "http://1.2.3.4:8080")

    def test_hostname_proxy_keeps_host(self):
        opt = self._build("http://u:p@ft-us.lazycat.cv:16611")
        self.assertEqual(opt["server"], "http://ft-us.lazycat.cv:16611")
        self.assertEqual(opt["username"], "u")

    def test_empty_proxy_returns_none(self):
        self.assertIsNone(self._build(""))
        self.assertIsNone(self._build("   "))


class FillFallbackTest(unittest.TestCase):
    """指针被遮挡时要退回 DOM 写入，不能直接失败。

    CloakBrowser 的 humanize 把 fill 变成真实点击，国家下拉重渲染时会盖住
    下方输入框拦截指针，表现为 fill 超时。
    """

    class Page:
        def __init__(self, fill_ok=True, dom_ok=True):
            self.fill_ok = fill_ok
            self.dom_ok = dom_ok
            self.dom_calls = []

        def locator(self, _sel):
            return self

        @property
        def first(self):
            return self

        def scroll_into_view_if_needed(self, timeout=0):
            pass

        def fill(self, _sel, _val):
            if not self.fill_ok:
                raise TimeoutError("intercepts pointer events")

        def evaluate(self, _script, args):
            self.dom_calls.append(tuple(args))
            return self.dom_ok

    def test_normal_fill_does_not_touch_dom(self):
        from core.vcc_checkout_pay import _fill
        page = self.Page(fill_ok=True)
        filled = {}
        _fill(page, "#city", "Anchorage", label="billing_city", filled=filled)
        self.assertTrue(filled["billing_city"])
        self.assertEqual(page.dom_calls, [])

    def test_pointer_blocked_falls_back_to_dom(self):
        from core.vcc_checkout_pay import _fill
        page = self.Page(fill_ok=False, dom_ok=True)
        filled = {}
        _fill(page, "#city", "Anchorage", label="billing_city", filled=filled)
        self.assertTrue(filled["billing_city"])
        self.assertEqual(page.dom_calls, [("#city", "Anchorage")])

    def test_required_field_raises_when_both_paths_fail(self):
        from core.vcc_checkout_pay import VccPayError, _fill
        page = self.Page(fill_ok=False, dom_ok=False)
        with self.assertRaises(VccPayError):
            _fill(page, "#city", "Anchorage", label="billing_city", filled={})

    def test_optional_field_is_skipped_when_both_fail(self):
        from core.vcc_checkout_pay import _fill
        page = self.Page(fill_ok=False, dom_ok=False)
        filled = {}
        _fill(page, "#line2", "Apt 1", label="billing_line2",
              filled=filled, required=False)
        self.assertFalse(filled["billing_line2"])


class ExactFillTest(unittest.TestCase):
    """卡号这类字段必须回读校验。

    humanize 逐字输入会和 Stripe 的自动插空格抢光标，导致字符顺序错乱甚至
    丢位（实测把 5556597902158361 填成了 5556 5978 9021 5836），页面直接
    报卡号无效。只看"没抛异常"是不够的。
    """

    class Page:
        def __init__(self, typed_result, dom_result=None):
            self.typed_result = typed_result
            self.dom_result = dom_result
            self.current = ""
            self.dom_calls = 0

        def locator(self, _sel):
            return self

        @property
        def first(self):
            return self

        def scroll_into_view_if_needed(self, timeout=0):
            pass

        def fill(self, _sel, _val):
            self.current = self.typed_result

        def input_value(self, _sel, timeout=0):
            return self.current

        def evaluate(self, _script, args):
            self.dom_calls += 1
            if self.dom_result is None:
                return False
            self.current = self.dom_result
            return True

    GOOD = "5556597902158361"
    SCRAMBLED = "5556 5978 9021 5836"

    def test_scrambled_card_triggers_dom_rewrite(self):
        from core.vcc_checkout_pay import _fill
        page = self.Page(typed_result=self.SCRAMBLED, dom_result=self.GOOD)
        filled = {}
        _fill(page, "#cardNumber", self.GOOD, label="card_number",
              filled=filled, exact=True)
        self.assertTrue(filled["card_number"])
        self.assertEqual(page.dom_calls, 1)

    def test_correct_typing_skips_dom(self):
        from core.vcc_checkout_pay import _fill
        # Stripe 会插空格，比较时只看数字
        page = self.Page(typed_result="5556 5979 0215 8361")
        filled = {}
        _fill(page, "#cardNumber", self.GOOD, label="card_number",
              filled=filled, exact=True)
        self.assertTrue(filled["card_number"])
        self.assertEqual(page.dom_calls, 0)

    def test_raises_when_dom_rewrite_also_wrong(self):
        from core.vcc_checkout_pay import VccPayError, _fill
        page = self.Page(typed_result=self.SCRAMBLED, dom_result="1234")
        with self.assertRaises(VccPayError):
            _fill(page, "#cardNumber", self.GOOD, label="card_number",
                  filled={}, exact=True)

    def test_non_exact_field_accepts_whatever_typed(self):
        from core.vcc_checkout_pay import _fill
        page = self.Page(typed_result="anything")
        filled = {}
        _fill(page, "#billingName", "ONeal Di", label="billing_name",
              filled=filled)
        self.assertTrue(filled["billing_name"])
        self.assertEqual(page.dom_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
