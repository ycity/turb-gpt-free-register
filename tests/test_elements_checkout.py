# -*- coding: utf-8 -*-
"""单席位结账页（Elements 变体）适配。

和 2 席的 Stripe 托管页是两套页面，实采差异（2026-11 验证）：
    classic   pay.openai.com/c/pay/cs_live_...  匿名可访问，卡号是主文档 input
    elements  chatgpt.com/checkout/openai_llc/  要登录，卡号在 Stripe iframe

踩过的坑都在这里锁住，别回退。
"""
import ast
import inspect
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
EL = (ROOT / "core" / "elements_checkout.py").read_text(encoding="utf-8")
PS = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")


def _fn_src(mod, name):
    """只取函数体代码，剥掉 docstring，避免注释里的词被断言命中。"""
    fn = getattr(mod, name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    return ast.unparse(node)


class DetectTest(unittest.TestCase):
    def test_recognises_single_seat_url(self):
        from core.elements_checkout import is_elements_checkout
        self.assertTrue(is_elements_checkout(
            "https://chatgpt.com/checkout/openai_llc/oaics_abc123"))

    def test_rejects_stripe_hosted_url(self):
        """2 席的 Stripe 长链必须走原来的 classic 路径。"""
        from core.elements_checkout import is_elements_checkout
        self.assertFalse(is_elements_checkout(
            "https://pay.openai.com/c/pay/cs_live_abc123"))

    def test_handles_empty(self):
        from core.elements_checkout import is_elements_checkout
        self.assertFalse(is_elements_checkout(""))
        self.assertFalse(is_elements_checkout(None))


class SelectorTest(unittest.TestCase):
    def test_card_fields_have_id_and_name_fallback(self):
        """id 变了还能靠 name 兜底，少一次线上事故。"""
        from core import elements_checkout as e
        for sel in (e.SEL_EL_NUMBER, e.SEL_EL_EXPIRY, e.SEL_EL_CVC):
            self.assertIn("#payment-", sel)
            self.assertIn("input[name=", sel)

    def test_submit_is_generic_not_testid(self):
        """这个页面的提交按钮没有 data-testid，用 classic 的选择器是 0 命中。"""
        from core.elements_checkout import SEL_EL_SUBMIT
        self.assertEqual(SEL_EL_SUBMIT, "button[type='submit']")
        self.assertNotIn("testid", SEL_EL_SUBMIT)

    def test_classic_selectors_untouched(self):
        """不能顺手改了 classic 的选择器，2 席还在用。"""
        from core.vcc_checkout_pay import SEL_CARD_NUMBER, SEL_SUBMIT
        self.assertEqual(SEL_CARD_NUMBER, "#cardNumber")
        self.assertIn("hosted-payment-submit-button", SEL_SUBMIT)


class FrameTest(unittest.TestCase):
    def test_waits_for_field_not_just_frame(self):
        """同源下有十几个 stripe iframe，只有带卡号输入框的那个是对的。"""
        from core import elements_checkout as e
        src = _fn_src(e, "find_elements_frame")
        self.assertIn("elements-inner", e._ELEMENTS_FRAME)
        self.assertIn("query_selector", src)
        self.assertIn("SEL_EL_NUMBER", src)

    def test_has_deadline(self):
        """iframe 是动态插的，必须轮询等待而不是查一次就放弃。"""
        from core import elements_checkout as e
        src = _fn_src(e, "find_elements_frame")
        self.assertIn("deadline", src)

    def test_raises_with_reason(self):
        from core import elements_checkout as e
        src = _fn_src(e, "find_elements_frame")
        self.assertIn("raise RuntimeError", src)


class FillTest(unittest.TestCase):
    def test_expiry_uses_two_digit_year(self):
        """Stripe 的 MM / YY 框只收两位，四位年会被截断成错值。"""
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_card")
        self.assertIn("[-2:]", src)

    def test_every_field_is_read_back(self):
        """Stripe 自动插空格并抢光标，不回读就不知道填错了。"""
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_card")
        self.assertGreaterEqual(src.count("raise RuntimeError"), 3)

    def test_compares_digits_only(self):
        """回读值带空格和斜杠，必须只比数字。"""
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_card")
        self.assertIn("_digits(got)", src)

    def test_digits_strips_formatting(self):
        from core.elements_checkout import _digits
        self.assertEqual(_digits("4242 4242 4242 4242"), "4242424242424242")
        self.assertEqual(_digits("03 / 30"), "0330")


class TotalTest(unittest.TestCase):
    def test_multilingual_labels(self):
        """页面语言跟随出口，实测出现过日语，只认英文会读不到金额。"""
        from core.elements_checkout import _TOTAL_LABELS
        joined = " ".join(_TOTAL_LABELS)
        self.assertIn("due today", joined)
        self.assertIn("今日", joined)

    def test_returns_none_when_unreadable(self):
        """读不到金额要如实返回 None，不能猜一个数字去过闸门。"""
        from core import elements_checkout as e
        src = _fn_src(e, "read_elements_total")
        self.assertIn("return ('', None)", src.replace('"', "'"))

    def test_reuses_existing_parser(self):
        from core import elements_checkout as e
        self.assertIn("parse_amount", _fn_src(e, "read_elements_total"))


class SeatGuardTest(unittest.TestCase):
    def test_reads_seat_quantity(self):
        from core import elements_checkout as e
        src = _fn_src(e, "read_seat_quantity")
        self.assertIn("SEL_EL_SEATS", src)
        self.assertIn("standard seat", src)

    def test_blocks_when_more_than_one_seat(self):
        """这条链路是"建 2 席再改 1 席"，update 没生效就会按 2 席扣款。"""
        i = PS.index("def _fill_elements_checkout")
        j = PS.index("def _screenshot_safe", i)
        body = PS[i:j]
        self.assertIn("seats > 1", body)
        self.assertIn("raise VccPayError", body)


class LoginTest(unittest.TestCase):
    def test_login_before_goto(self):
        """没登录态会被 302 到登录页，连表单都看不到。"""
        i = PS.index("use_elements = is_elements_checkout")
        j = PS.index("page.goto(checkout_url", i)
        self.assertIn("_login_cookies_for", PS[i:j])

    def test_cookie_prefix_rules(self):
        """__Host- 不能带 domain，__Secure- 必须 secure，否则整批注入被拒。"""
        from core import vcc_pay_session as v
        src = _fn_src(v, "_cookie_for_playwright")
        self.assertIn("__Host-", src)
        self.assertIn("'secure': True", src.replace('"', "'"))
        self.assertIn("url", src)

    def test_login_has_retry(self):
        """这些出口的 403 是间歇性的，一次失败就放弃等于白做。"""
        from core import vcc_pay_session as v
        src = _fn_src(v, "_login_cookies_for")
        self.assertIn("for rd in range", src)

    def test_requires_session_token(self):
        """登录"成功"但没拿到 session-token 等于没登录。"""
        from core import vcc_pay_session as v
        src = _fn_src(v, "_login_cookies_for")
        self.assertIn("session-token", src)

    def test_falls_back_to_per_cookie(self):
        i = PS.index("cookie 整批注入失败")
        self.assertIn("add_cookies([c])", PS[i:i + 600])

    def test_detects_redirect_to_login(self):
        """注入后仍跳登录页要明确报错，不能闷头去填不存在的表单。"""
        self.assertIn('"auth/login" in str(page.url', PS)


class RoutingTest(unittest.TestCase):
    def test_classic_path_still_used(self):
        """2 席必须还走 fill_checkout，不能被新分支吃掉。"""
        i = PS.index("tmp = PayResult()")
        seg = PS[i:i + 700]
        self.assertIn("_fill_elements_checkout", seg)
        self.assertIn("fill_checkout(page", seg)

    def test_total_reader_switches(self):
        i = PS.index("from core.elements_checkout import read_elements_total")
        seg = PS[i:i + 900]
        self.assertIn("read_elements_total", seg)
        self.assertIn("read_total(page)", seg)

    def test_submit_selector_switches(self):
        i = PS.index("if use_elements:\n            from core.elements_checkout import SEL_EL_SUBMIT")
        seg = PS[i:i + 400]
        self.assertIn("SEL_EL_SUBMIT", seg)
        self.assertIn("SEL_SUBMIT", seg)

    def test_wait_settled_gets_matching_selector(self):
        """传错按钮会一直查不到元素，误判成"提交已结束"。"""
        from core import vcc_pay_session as v
        src = _fn_src(v, "_wait_settled")
        self.assertIn("submit_sel or SEL_SUBMIT", src)
        self.assertEqual(PS.count("submit_sel=sel_submit"), 2)


class CurrencyFallbackTest(unittest.TestCase):
    """页面只渲染货币符号，没有币种码（实采确认 HTML 里没有，
    也没有 __NEXT_DATA__）。闸门在 VCC_EXPECTED_CURRENCY 非空时
    读不到币种就会拦住提交，所以要用下单币种兜底。"""

    def test_symbol_match_accepts_dollar_for_usd(self):
        from core.elements_checkout import _symbol_matches
        self.assertTrue(_symbol_matches("Due today $25.00", "USD"))

    def test_symbol_match_rejects_wrong_symbol(self):
        """币种被整体换掉时要能挡住，不能无条件兜底。"""
        from core.elements_checkout import _symbol_matches
        self.assertFalse(_symbol_matches("Due today €25.00", "USD"))
        self.assertFalse(_symbol_matches("Due today ¥2500", "USD"))

    def test_symbol_match_rejects_unknown_currency(self):
        """没登记的币种不敢断言，按识别不出处理。"""
        from core.elements_checkout import _symbol_matches
        self.assertFalse(_symbol_matches("$25.00", "XYZ"))

    def test_egp_symbols(self):
        from core.elements_checkout import _symbol_matches
        self.assertTrue(_symbol_matches("EGP 1,840.00", "EGP"))

    def test_fallback_is_conditional(self):
        """兜底必须先验符号，否则等于架空了币种校验。"""
        from core import elements_checkout as e
        src = _fn_src(e, "read_elements_total")
        self.assertIn("_symbol_matches", src)
        i = src.index("fallback_currency")
        self.assertIn("_symbol_matches", src[i:])

    def test_caller_passes_expected_currency(self):
        i = PS.index("read_elements_total(")
        self.assertIn("fallback_currency", PS[i:i + 300])
        self.assertIn("VCC_EXPECTED_CURRENCY", PS[i:i + 300])


class BillingAddressTest(unittest.TestCase):
    """账单地址在另一个 Stripe iframe 里，渲染比卡号晚。

    页面默认国家跟着 checkout session 的 billing_details 走（按 EG 建单
    拿 EGP 定价），但卡是美国卡，不改会被发卡行判地址不符而拒付。
    实测改成 US/AK 后税从 EGP128.80 变 0，总额 1048.80 -> 920.00。
    """

    def test_address_selectors_exist(self):
        from core import elements_checkout as e
        for sel in (e.SEL_AD_NAME, e.SEL_AD_COUNTRY, e.SEL_AD_LINE1,
                    e.SEL_AD_CITY, e.SEL_AD_STATE, e.SEL_AD_POSTAL):
            self.assertIn("billingAddress-", sel)
            self.assertIn("name=", sel)

    def test_address_frame_is_separate(self):
        """和卡号不在同一个 iframe，只能靠字段区分。"""
        from core import elements_checkout as e
        src = _fn_src(e, "find_address_frame")
        self.assertIn("SEL_AD_COUNTRY", src)
        self.assertNotIn("SEL_EL_NUMBER", src)

    def test_scrolls_to_render(self):
        """地址区块在页面下方，不滚过去可能一直不渲染。

        必须用 JS 滚而不是 page.mouse.wheel：CloakBrowser 开着 humanize，
        鼠标操作会变成带轨迹的真实动作，实测卡过 5 分钟且外层 timeout
        管不到它（线上 #75 就是这么卡住的）。
        """
        from core import elements_checkout as e
        src = _fn_src(e, "find_address_frame")
        self.assertIn("scrollTo", src)
        self.assertNotIn("mouse.wheel", src)

    def test_country_selected_first(self):
        """州下拉按国家重建，顺序反了选不到州。"""
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_address")
        i = src.index("SEL_AD_COUNTRY")
        self.assertLess(i, src.index("SEL_AD_STATE"))
        self.assertLess(i, src.index("SEL_AD_LINE1"))

    def test_state_failure_is_fatal(self):
        """州直接决定税额，选不中必须停，不能带着错地址提交。"""
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_address")
        i = src.index("SEL_AD_STATE")
        self.assertIn("raise RuntimeError", src[i:])
        self.assertIn("税额", src)

    def test_state_accepts_code_or_label(self):
        """配置里写 AK 或 Alaska 都要能选中。

        改用 JS 后不再是 select_option 的 value/label 参数，
        而是在 options 里按 value 和文本两头匹配。
        """
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_address")
        self.assertIn("o.value", src)
        self.assertIn("o.text", src)

    def test_required_fields_enforced(self):
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_address")
        self.assertIn("缺少必填项", src)

    def test_called_after_card(self):
        """先填卡再填地址：地址区块要卡填完才渲染出来。"""
        i = PS.index("def _fill_elements_checkout")
        j = PS.index("def _screenshot_safe", i)
        body = PS[i:j]
        self.assertLess(body.index("fill_elements_card"),
                        body.index("fill_elements_address("))

    def test_billing_passed_in(self):
        """billing 必须从支付会话传进来，不能在函数里另读配置。"""
        self.assertIn("_fill_elements_checkout(page, card=card, billing=billing",
                      PS)
        i = PS.index("def _fill_elements_checkout")
        self.assertIn("billing: dict", PS[i:i + 200])

    def test_total_read_after_address(self):
        """改地址会重算税，必须填完地址再读金额。"""
        i = PS.index("_fill_elements_checkout(page, card=card")
        j = PS.index("read_elements_total", i)
        self.assertGreater(j, i)


class HumanizeBypassTest(unittest.TestCase):
    """CloakBrowser 的 humanize 会把 Playwright 的交互 API 改写成
    真实鼠标/键盘动作，在 Stripe iframe 里会无限阻塞，而且
    Playwright 的 timeout 参数管不住它。

    线上 #75 两次卡死 5 分钟都是这个原因：
      第一次 page.mouse.wheel（滚动找地址区块）
      第二次 frame.select_option（选账单国家）
    对照实验：裸 Playwright 下 select_option 只要 0.2s。
    """

    def test_no_select_option_call(self):
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_address")
        self.assertNotIn("select_option", src)

    def test_no_frame_fill_call(self):
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_address")
        self.assertNotIn("frame.fill(", src)

    def test_uses_js_helpers(self):
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_address")
        self.assertIn("_js_select", src)
        self.assertIn("_js_fill", src)

    def test_js_select_uses_native_setter(self):
        """React 受控组件只改 value 不会触发状态更新。"""
        from core import elements_checkout as e
        src = _fn_src(e, "_js_select")
        self.assertIn("getOwnPropertyDescriptor", src)
        self.assertIn("'change'", src.replace('"', "'"))

    def test_js_fill_dispatches_input(self):
        from core import elements_checkout as e
        src = _fn_src(e, "_js_fill")
        self.assertIn("'input'", src.replace('"', "'"))
        self.assertIn("'change'", src.replace('"', "'"))

    def test_card_fields_still_typed(self):
        """卡号三件套有 Stripe 的格式化逻辑，必须逐字输入，
        不能图快改成 JS 设值。"""
        from core import elements_checkout as e
        src = _fn_src(e, "_fill_frame_field")
        self.assertIn("el.type(", src)

    def test_country_result_verified(self):
        """JS 设值不会抛异常，必须回读确认真的改了。"""
        from core import elements_checkout as e
        src = _fn_src(e, "fill_elements_address")
        i = src.index("_js_select")
        self.assertIn("!= country", src[i:i + 300].replace("!==", "!="))


class TotalLabelTest(unittest.TestCase):
    """读金额不能只靠关键词：页面语言跟随出口，总有没见过的。"""

    def test_traditional_chinese_included(self):
        """实测台湾出口是 zh-TW「今天應付金額」，和简体字都不一样。"""
        from core.elements_checkout import _TOTAL_LABELS
        joined = " ".join(_TOTAL_LABELS)
        self.assertIn("今天應付金額", joined)
        self.assertIn("今日应付", joined)

    def test_has_structural_fallback(self):
        """关键词漏了也要能读到，否则闸门会拦住正常支付。"""
        from core import elements_checkout as e
        src = _fn_src(e, "read_elements_total")
        self.assertIn("_read_total_by_structure", src)

    def test_fallback_skips_discount_lines(self):
        """折扣是负数，不可能是应付额。"""
        from core import elements_checkout as e
        src = _fn_src(e, "_read_total_by_structure")
        self.assertIn("startswith('-')", src.replace('"', "'"))

    def test_fallback_takes_last_amount(self):
        """摘要里金额按 小计 -> 税 -> 应付 排列，取最后一个。"""
        from core import elements_checkout as e
        src = _fn_src(e, "_read_total_by_structure")
        self.assertIn("cands[-1]", src)

    def test_fallback_excludes_unit_prices(self):
        """"EGP 1,150/月" 是单价不是应付额，正则要排除掉。"""
        from core import elements_checkout as e
        src = _fn_src(e, "_read_total_by_structure")
        self.assertIn("$", src)   # 正则带 $ 锚定结尾

    def test_fallback_returns_none_when_unsure(self):
        from core import elements_checkout as e
        src = _fn_src(e, "_read_total_by_structure")
        self.assertGreaterEqual(src.count("return ('', None)"),
                                2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
