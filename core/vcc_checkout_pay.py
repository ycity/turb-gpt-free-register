# -*- coding: utf-8 -*-
"""用 VCC 虚拟卡填写 Stripe 结账页并（可选）提交。

页面结构来自实采 HTML：classic 变体下卡号/有效期/CVV 是页面顶层 DOM 的
普通 input（#cardNumber / #cardExpiry / #cardCvc），不在 Stripe Elements
iframe 里，因此直接 fill 即可。guacamole 变体 DOM 不同，这里会显式拒绝而
不是盲填。

填写顺序有依赖，不能随意调整：
    1. 先选国家 —— 省份下拉会按国家重新渲染
    2. 再填州/市/街道/邮编
    3. 地址填完后页面才会重算税，此时读到的总额才是最终应付额
    4. 最后校验金额闸门，通过才提交

资金安全：两道闸门（等值 + 上限）任一不过都停在提交前并截图。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# classic 变体选择器
SEL_FORM = "form#payment-form"
SEL_EMAIL = "#email"
SEL_CARD_NUMBER = "#cardNumber"
SEL_CARD_EXPIRY = "#cardExpiry"
SEL_CARD_CVC = "#cardCvc"
SEL_BILLING_NAME = "#billingName"
SEL_COUNTRY = "select#billingCountry"
SEL_STATE = "select#billingAdministrativeArea"
SEL_LINE1 = "#billingAddressLine1"
SEL_LINE2 = "#billingAddressLine2"
SEL_CITY = "#billingLocality"
SEL_POSTAL = "#billingPostalCode"
SEL_TERMS = "#termsOfServiceConsentCheckbox"
SEL_SAVE_PM = "#optInSetupFutureUsageCheckbox"
SEL_SUBMIT = 'button[data-testid="hosted-payment-submit-button"]'
SEL_TOTAL = "#OrderDetails-TotalAmount"
SEL_TOTAL_FALLBACK = '[data-testid="product-summary-total-amount"]'

# 三位字母货币码 + 金额，例如 "EGP 1,311.00" / "US$1,150.00" / "¥1,150"
# 负号可能出现在币种之前（-EGP 1,150.00）或数字紧前（EGP -1,150.00），都要认。
_AMOUNT_RE = re.compile(
    r"(?P<neg1>-)?\s*(?P<cur>[A-Z]{3})?\s*[^\d\-]*(?P<neg2>-)?(?P<num>[\d,.\u00a0\u202f ]+)")

# 货币符号 → 币种码。页面可能只给符号而不给三字母码。
_SYMBOL_CURRENCY = (
    ("US$", "USD"), ("A$", "AUD"), ("CA$", "CAD"), ("HK$", "HKD"),
    ("NT$", "TWD"), ("S$", "SGD"), ("R$", "BRL"), ("₹", "INR"),
    ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"), ("₩", "KRW"), ("₺", "TRY"),
)


class VccPayError(RuntimeError):
    """支付流程失败。"""


class AmountGateError(VccPayError):
    """金额闸门未通过，已阻止提交。"""


@dataclass
class PayResult:
    ok: bool = False
    submitted: bool = False
    stage: str = ""
    message: str = ""
    currency: str = ""
    amount: float | None = None
    expected: float | None = None
    card_mask: str = ""
    screenshots: list[str] = field(default_factory=list)
    filled: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "submitted": self.submitted, "stage": self.stage,
            "message": self.message, "currency": self.currency,
            "amount": self.amount, "expected": self.expected,
            "card_mask": self.card_mask, "screenshots": list(self.screenshots),
            "filled": dict(self.filled),
        }


def parse_amount(text: str) -> tuple[str, float | None]:
    """从页面金额文本解析出 (币种, 数值)。"""
    if not text:
        return "", None
    cleaned = text.replace("\u00a0", " ").replace("\u202f", " ").strip()
    match = _AMOUNT_RE.search(cleaned)
    if not match:
        return "", None
    currency = (match.group("cur") or "").strip()
    negative = bool(match.group("neg1") or match.group("neg2"))
    raw = (match.group("num") or "").strip()
    # 去掉千分位空格，再判断小数点/逗号写法
    raw = raw.replace(" ", "")
    if "," in raw and "." in raw:
        # 1,311.00 或 1.311,00 —— 以最后出现的符号为小数点
        raw = raw.replace(",", "") if raw.rfind(".") > raw.rfind(",") else \
            raw.replace(".", "").replace(",", ".")
    elif raw.count(",") == 1 and len(raw.split(",")[-1]) in (1, 2):
        raw = raw.replace(",", ".")      # 1150,00
    else:
        raw = raw.replace(",", "")       # 1,150
    if not currency:
        upper = cleaned.upper()
        for code in ("EGP", "USD", "EUR", "GBP", "JPY", "HKD", "SGD", "CNY",
                     "AUD", "CAD", "TWD", "KRW", "INR", "BRL", "TRY"):
            if code in upper:
                currency = code
                break
    if not currency:
        for symbol, code in _SYMBOL_CURRENCY:
            if symbol in cleaned:
                currency = code
                break
    try:
        value = float(raw)
    except ValueError:
        return currency, None
    return currency, -value if negative else value


def check_amount_gate(amount: float | None, currency: str, *,
                      expected: float, expected_currency: str,
                      tolerance: float, max_charge: float) -> None:
    """两道资金闸门。不通过就抛 AmountGateError，调用方必须不提交。

    一切不确定都按"不放行"处理：金额解析不出、币种识别不出都会拦住，
    绝不因为读不到就当成通过。
    """
    if amount is None:
        raise AmountGateError("无法从页面解析应付金额，已阻止提交")
    if max_charge > 0 and amount > max_charge + 1e-9:
        raise AmountGateError(
            f"应付金额 {currency or '?'} {amount:.2f} 超过上限 {max_charge:.2f}，已阻止提交")
    if expected_currency:
        if not currency:
            # 读不到币种时不能放行：同样的数字换个币种金额完全不同。
            raise AmountGateError(
                f"无法识别页面币种（期望 {expected_currency}），已阻止提交")
        if currency.upper() != expected_currency.upper():
            raise AmountGateError(
                f"页面币种 {currency} 与期望 {expected_currency} 不一致，已阻止提交")
    if expected > 0 and abs(amount - expected) > max(tolerance, 0.0) + 1e-9:
        raise AmountGateError(
            f"应付金额 {currency or '?'} {amount:.2f} 不等于期望 {expected:.2f}，已阻止提交"
            "（账单地址变化会影响税费，请核对地址与优惠码）")


def detect_variant(page: Any) -> str:
    try:
        value = page.get_attribute("html", "data-checkout-variant") or ""
    except Exception:  # noqa: BLE001
        value = ""
    return value.strip().lower()


def _screenshot(page: Any, out_dir: Path, name: str, sink: list[str]) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{int(time.time())}_{name}.png"
        page.screenshot(path=str(path), full_page=True)
        sink.append(str(path))
        logger.info("[vcc-pay] 截图 %s", path)
    except Exception as exc:  # noqa: BLE001 - 截图失败不该打断支付
        logger.debug("[vcc-pay] 截图失败：%s", exc)


def _field_timeout_ms() -> int:
    """单个字段操作的超时（毫秒）。

    被遮挡的 fill/select 注定失败，之后立刻走 DOM 直写，所以不该拿
    VCC_PAGE_TIMEOUT 那 60 秒去等——十几个字段里几个被遮挡就是好几分钟。
    """
    from config import vcc as _cfg
    try:
        val = float(getattr(_cfg, "VCC_FIELD_TIMEOUT", 6.0) or 6.0)
    except (TypeError, ValueError):
        val = 6.0
    return int(max(1.0, val) * 1000)


def _scroll_into_view(page: Any, selector: str) -> None:
    """把元素滚进视口。

    CloakBrowser 的 humanize 会把 fill 变成"移鼠标 + 点击聚焦 + 逐字输入"，
    元素不在视口里就点不到，表现为 fill 超时；裸 Playwright 直接写 DOM 所以
    不受影响。这里统一先滚动，两种浏览器都安全。
    """
    try:
        page.locator(selector).first.scroll_into_view_if_needed(timeout=5000)
    except Exception:  # noqa: BLE001 - 滚不动就让后续操作自己报错
        pass


def _fill_via_dom(page: Any, selector: str, value: str) -> bool:
    """绕过指针直接写入并派发事件。

    humanize 模式下相邻控件（例如国家下拉）可能盖住输入框拦截指针事件，
    真实点击永远落不上去。此时退回 DOM 写入：值和事件都是真的，只是少了
    鼠标轨迹，对这种普通文本框没有风控意义。

    复用旧长链时 Stripe 会把已填过的 email / 地址锁成 disabled+readonly，
    直接 setter 写入会被 React 回滚，所以先摘掉锁、写完再还原。

    写完只确认"值不为空"，不做严格相等：Stripe 会给卡号自动插空格
    （5378727141745290 → 5378 7271 4174 5290），严格比会把成功判成失败。
    精确校验由调用方的 _value_matches 做，它只比数字。
    """
    try:
        return bool(page.evaluate(
            """([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                // 复用长链时字段被锁死，解锁后才写得进去
                const wasDisabled = el.disabled;
                const wasReadOnly = el.readOnly;
                if (wasDisabled) el.disabled = false;
                if (wasReadOnly) el.readOnly = false;
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                // 只看写进去没有：格式化会改变字面值，严格相等必然失败
                const ok = String(el.value || '').length > 0;
                // 还原原状：锁是 Stripe 自己加的，留着改动可能触发它的校验
                if (wasDisabled) el.disabled = true;
                if (wasReadOnly) el.readOnly = true;
                return ok;
            }""", [selector, value]))
    except Exception:  # noqa: BLE001
        return False


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def _value_matches(page: Any, selector: str, expected: str) -> bool:
    """校验输入框里的值是否就是我们想填的。

    Stripe 会给卡号自动插空格，所以只比数字；humanize 逐字输入时会和这个
    自动格式化抢光标，导致字符顺序错乱甚至丢位，必须填完回读确认。
    """
    try:
        actual = page.input_value(selector, timeout=3000) or ""
    except Exception:  # noqa: BLE001
        return False
    want = _digits(expected)
    if want:
        return _digits(actual) == want
    return actual.strip() == str(expected).strip()


def _fill(page: Any, selector: str, value: str, *, label: str,
          filled: dict[str, bool], required: bool = True,
          exact: bool = False) -> None:
    """填一个输入框。

    exact=True 时填完会回读校验，值对不上就改用 DOM 直写。卡号、有效期、
    CVV 这类字段差一位就作废，不能只看"没抛异常"。
    """
    if not value:
        if required:
            raise VccPayError(f"{label} 为空，无法填写")
        filled[label] = False
        return
    try:
        _scroll_into_view(page, selector)
        page.fill(selector, value, timeout=_field_timeout_ms())
        if not exact or _value_matches(page, selector, value):
            filled[label] = True
            return
        logger.warning("[vcc-pay] %s 填写后校验不符，改用 DOM 直写", label)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[vcc-pay] %s 常规填写失败，改走 DOM：%s", label, type(exc).__name__)

    if _fill_via_dom(page, selector, value):
        if exact and not _value_matches(page, selector, value):
            if required:
                raise VccPayError(f"填写 {label} 后校验不符，已停止以免提交错误信息")
            filled[label] = False
            return
        logger.info("[vcc-pay] %s 改用 DOM 写入成功", label)
        filled[label] = True
        return
    if required:
        actual = ""
        try:
            actual = page.input_value(selector, timeout=2000) or ""
        except Exception:  # noqa: BLE001 - 读不到就不带这段
            pass
        raise VccPayError(
            f"填写 {label} 失败：DOM 写入未生效"
            + (f"（页面当前值 {actual!r}）" if actual else "（页面当前为空）"))
    filled[label] = False


def _expand_manual_address(page: Any) -> bool:
    """复用旧长链时把折叠的地址表单展开。

    上一个长链填过地址的话，Stripe 会把账单地址收成一行只读摘要，底下
    line1/city/state/postal 这些 input 压根没渲染，光解 disabled 没用。
    页面提供「Enter address manually」入口，点开才会出现完整表单。

    返回是否执行了展开动作；表单本来就是展开的返回 False。
    """
    try:
        if page.locator(SEL_LINE1).first.is_visible(timeout=1200):
            return False
    except Exception:  # noqa: BLE001 - 不可见或不存在都按需要展开处理
        pass

    for name in ("Enter address manually", "Enter address",
                 "手动输入地址", "手动填写地址"):
        try:
            link = page.get_by_text(name, exact=False).first
            if not link.is_visible(timeout=800):
                continue
            link.click(timeout=_field_timeout_ms())
            page.wait_for_selector(SEL_LINE1, timeout=_field_timeout_ms())
            logger.info("[vcc-pay] 复用长链地址被折叠，已点开「%s」", name)
            return True
        except Exception:  # noqa: BLE001 - 换下一个文案
            continue

    # 文案没匹配上就直接点 DOM 里那个入口
    try:
        clicked = page.evaluate(
            """() => {
                const hit = [...document.querySelectorAll('a,button,[role=button]')]
                    .find(e => /enter address manually|手动(输入|填写)地址/i
                        .test((e.textContent || '').trim()));
                if (!hit) return false;
                hit.click();
                return true;
            }""")
        if clicked:
            page.wait_for_selector(SEL_LINE1, timeout=_field_timeout_ms())
            logger.info("[vcc-pay] 复用长链地址被折叠，已通过 DOM 点开手动地址")
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _select_via_dom(page: Any, selector: str, value: str) -> bool:
    """绕过指针直接选中下拉项。

    和 _fill_via_dom 同理：humanize 要真实点击，被遮挡就永远点不上。
    按 value 找不到时退而按可见文本匹配。复用旧长链时下拉同样会被锁，
    所以也要先摘 disabled。
    """
    try:
        return bool(page.evaluate(
            """([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                const wasDisabled = el.disabled;
                if (wasDisabled) el.disabled = false;
                const want = String(val).trim().toLowerCase();
                let hit = -1;
                for (let i = 0; i < el.options.length; i++) {
                    const o = el.options[i];
                    if (String(o.value).trim().toLowerCase() === want) { hit = i; break; }
                }
                if (hit < 0) {
                    for (let i = 0; i < el.options.length; i++) {
                        if (el.options[i].text.trim().toLowerCase() === want) { hit = i; break; }
                    }
                }
                if (hit < 0) { if (wasDisabled) el.disabled = true; return false; }
                el.selectedIndex = hit;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                if (wasDisabled) el.disabled = true;
                return true;
            }""", [selector, value]))
    except Exception:  # noqa: BLE001
        return False


def _select(page: Any, selector: str, value: str, *, label: str,
            filled: dict[str, bool], required: bool = True) -> None:
    if not value:
        filled[label] = False
        if required:
            raise VccPayError(f"{label} 为空，无法选择")
        return
    for attempt in (
        lambda: page.select_option(selector, value=value,
                                   timeout=_field_timeout_ms()),
        lambda: page.select_option(selector, label=value,
                                   timeout=_field_timeout_ms()),
    ):
        try:
            _scroll_into_view(page, selector)
            attempt()
            filled[label] = True
            return
        except Exception:  # noqa: BLE001
            continue

    if _select_via_dom(page, selector, value):
        logger.info("[vcc-pay] %s 改用 DOM 选中成功（指针被遮挡）", label)
        filled[label] = True
        return
    if required:
        raise VccPayError(
            f"选择 {label}={value} 失败，页面可能不接受该取值")
    logger.debug("[vcc-pay] 选填下拉 %s 跳过", label)
    filled[label] = False


def _selected_state(page: Any) -> dict[str, str]:
    """读州/省下拉当前选中项的 value 和显示文字。

    Stripe 的州下拉 value 是代码（OR），label 是全称（Oregon）。校验必须两者
    都看，否则按全称选中时会被误判成没选上。
    """
    try:
        got = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const opt = el.selectedOptions && el.selectedOptions[0];
                return {
                    value: String(el.value || ''),
                    label: String(opt ? (opt.textContent || '') : '').trim(),
                };
            }""", SEL_STATE) or {}
    except Exception:  # noqa: BLE001
        got = {}
    return {"value": str(got.get("value") or ""), "label": str(got.get("label") or "")}


def _check(page: Any, selector: str, *, label: str,
           filled: dict[str, bool], required: bool) -> None:
    try:
        if page.is_checked(selector, timeout=_field_timeout_ms()):
            filled[label] = True
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        _scroll_into_view(page, selector)
        page.check(selector, timeout=_field_timeout_ms())
        filled[label] = True
        return
    except Exception:  # noqa: BLE001
        pass

    # 条款勾选是提交的前提，被遮挡时也要能勾上
    try:
        ok = bool(page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                if (!el.checked) { el.click(); }
                return !!el.checked;
            }""", selector))
    except Exception:  # noqa: BLE001
        ok = False
    if ok:
        logger.info("[vcc-pay] %s 改用 DOM 勾选成功（指针被遮挡）", label)
        filled[label] = True
        return
    if required:
        raise VccPayError(f"勾选 {label} 失败：指针被遮挡且 DOM 勾选未成功")
    logger.debug("[vcc-pay] 选填勾选 %s 跳过", label)
    filled[label] = False


def read_total(page: Any) -> tuple[str, float | None]:
    for selector in (SEL_TOTAL, SEL_TOTAL_FALLBACK):
        try:
            text = page.inner_text(selector, timeout=3000)
        except Exception:  # noqa: BLE001
            continue
        currency, amount = parse_amount(text)
        if amount is not None:
            return currency, amount
    return "", None


def _expired_text(page: Any) -> str:
    """读页面正文，命中过期标记就返回那段文字；否则空串。"""
    try:
        text = (page.inner_text("body", timeout=1500) or "").strip()
    except Exception:  # noqa: BLE001 - 读不到就当作还没渲染完
        return ""
    low = text.lower()
    return text if any(mark in low for mark in _EXPIRED_MARKERS) else ""


def _wait_form_or_fail(page: Any, cfg: Any) -> None:
    """等支付表单出现，但别为一条已经作废的长链白等满超时。

    过期的长链不会报错，只是页面渲染成一句 "you're all done here"，表单永远
    不出现。原先只在 wait_for_selector 超时之后才去看这段文字，于是每次都要
    先空等 VCC_PAGE_TIMEOUT（实测 60 秒，而页面本身 1 秒就加载完了）。
    这里改成边等边查：表单出现就立刻返回，识别出过期文案就立刻失败。
    """
    total = float(getattr(cfg, "VCC_FORM_TIMEOUT", 0) or 0)
    if total <= 0:
        total = float(getattr(cfg, "VCC_PAGE_TIMEOUT", 60.0) or 60.0)
    deadline = time.monotonic() + total
    step_ms = 700

    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        try:
            page.wait_for_selector(
                SEL_FORM, timeout=int(min(step_ms, remain * 1000)))
            return
        except Exception:  # noqa: BLE001 - 这一轮没等到，继续看是不是过期了
            pass
        hit = _expired_text(page)
        if hit:
            raise VccPayError(
                "结账会话已过期或已完成，支付表单不会再出现。"
                "这条长链已作废，需要重新生成（会消耗一个新的优惠码）")

    text = ""
    try:
        text = (page.inner_text("body") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    if not text:
        raise VccPayError(
            "结账页空白，页面资源没能加载完（常见于代理不稳定）。"
            "长链本身可能还有效，可稍后重试或换代理")
    raise VccPayError(f"等不到支付表单，页面当前显示：{text[:80]}")


def fill_checkout(page: Any, *, card: dict[str, str], email: str,
                  billing: dict[str, str], cfg: Any,
                  result: PayResult, out_dir: Path) -> None:
    """填写结账表单。不提交，不校验金额。"""
    variant = detect_variant(page)
    if variant and variant != "classic":
        raise VccPayError(
            f"结账页变体为 {variant}，当前仅适配 classic；已停止以免误填")
    result.stage = "fill"
    _wait_form_or_fail(page, cfg)

    filled = result.filled
    # 复用旧长链时 email 已被 Stripe 锁死且就是本账号的地址，覆盖不上也无妨，
    # 不该为它中断整条支付流程——地址才是必须填对的。
    _fill(page, SEL_EMAIL, email, label="email", filled=filled, required=False)
    # 支付三要素差一位就作废，且 Stripe 会自动插空格与逐字输入抢光标，必须回读校验
    _fill(page, SEL_CARD_NUMBER, card["number"], label="card_number",
          filled=filled, exact=True)
    _fill(page, SEL_CARD_EXPIRY, f"{card['exp_month']} / {card['exp_year']}",
          label="card_expiry", filled=filled, exact=True)
    _fill(page, SEL_CARD_CVC, card["cvc"], label="card_cvc",
          filled=filled, exact=True)
    _fill(page, SEL_BILLING_NAME, billing["name"], label="billing_name", filled=filled)

    # 复用旧长链时账单地址会被收成只读摘要，底下的输入框根本没渲染，
    # 必须先点开手动地址入口。地址错了税费和风控都不对，不能跳过。
    _expand_manual_address(page)

    # 国家必须先选：省份下拉按国家重新渲染，顺序反了会选不到州。
    # 重渲染期间下拉会盖住下方输入框拦截指针，humanize 模式下点不进去，
    # 所以等省份下拉真正重建完再填地址。
    _select(page, SEL_COUNTRY, billing["country"], label="billing_country", filled=filled)
    try:
        page.wait_for_function(
            """() => {
                const s = document.querySelector('select#billingAdministrativeArea');
                return !s || s.options.length > 1;
            }""", timeout=8000)
    except Exception:  # noqa: BLE001 - 等不到就按原节奏继续
        pass
    page.wait_for_timeout(800)

    _fill(page, SEL_LINE1, billing["line1"], label="billing_line1", filled=filled)
    _fill(page, SEL_LINE2, billing.get("line2", ""), label="billing_line2",
          filled=filled, required=False)
    _fill(page, SEL_CITY, billing["city"], label="billing_city", filled=filled)

    # 州/省直接决定税费（例如 US-AK 无销售税），不能静默跳过：
    # 配置里给了值就必须选中，否则宁可停下来报错，也不要提交一个错误的账单归属地。
    want_state = billing.get("state", "")
    if want_state:
        _select(page, SEL_STATE, want_state, label="billing_state",
                filled=filled, required=True)
        # 下拉的 value 是州代码（OR），label 才是全称（Oregon）。配置两种写法都
        # 允许，所以 value 和 label 任一对上就算选中；只比 value 会把按全称选中
        # 的情况误判成失败，白白作废一个已经消耗掉优惠码的长链。
        got = _selected_state(page)
        want = want_state.strip().casefold()
        if want not in (got["value"].strip().casefold(), got["label"].strip().casefold()):
            raise VccPayError(
                f"账单州/省未选中（期望 {want_state}，页面实际 "
                f"value={got['value'] or '空'} label={got['label'] or '空'}）。"
                "州/省会影响税费，已停止以免提交错误的账单地址")
    else:
        filled["billing_state"] = False

    _fill(page, SEL_POSTAL, billing.get("postal", ""), label="billing_postal",
          filled=filled, required=False, exact=True)

    if getattr(cfg, "VCC_SAVE_PAYMENT_METHOD", False):
        _check(page, SEL_SAVE_PM, label="save_payment_method",
               filled=filled, required=False)
    # OpenAI 商务条款是必填项，不勾无法提交
    if getattr(cfg, "VCC_ACCEPT_TERMS", True):
        _check(page, SEL_TERMS, label="accept_terms", filled=filled, required=True)

    # 最后复核支付三要素：中间任何一步的重渲染都可能把已填内容冲掉，
    # 带着错卡号提交会被判成无效卡，白费一次尝试。
    for selector, want, name in (
        (SEL_CARD_NUMBER, card["number"], "卡号"),
        (SEL_CARD_CVC, card["cvc"], "CVV"),
    ):
        if not _value_matches(page, selector, want):
            if not _fill_via_dom(page, selector, want) or \
                    not _value_matches(page, selector, want):
                raise VccPayError(f"{name}最终校验不符，已停止以免提交错误卡信息")
            logger.warning("[vcc-pay] %s 最终复核时重写了一次", name)

    # 地址落定后页面会重算税，等它稳下来再读金额
    page.wait_for_timeout(1500)
    _screenshot(page, out_dir, "filled", result.screenshots)


def build_proxy_option(proxy: str) -> dict[str, str] | None:
    """把代理串转成 Playwright 的 proxy 参数。

    Chromium 不认 server 里内嵌的 user:pass，必须拆成独立字段，否则所有请求
    都会静默卡住直到超时（表现为 Page.goto timeout，而不是认证失败）。
    """
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    if not parsed.hostname:
        return {"server": proxy}
    option = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}"
                        f"{':' + str(parsed.port) if parsed.port else ''}"}
    if parsed.username:
        option["username"] = parsed.username
        option["password"] = parsed.password or ""
    return option


#: Stripe checkout session 过期后页面上会出现的措辞
_EXPIRED_MARKERS = (
    "you're all done here",
    "you’re all done here",
    "checkout session has timed out",
    "this payment link is no longer",
    "no longer active",
)


def probe_checkout_link(checkout_url: str, *, proxy: str = "",
                        timeout: float = 30.0,
                        account: dict | None = None) -> dict[str, Any]:
    """只读地检查长链还能不能用来支付，不填表也不提交。

    结果分三种，处置完全不同：
      - usable：表单能渲染出来，可以直接支付
      - 会话过期：长链作废，只能重新生成（会再消耗优惠码）
      - 页面空白：长链多半还有效，是这个出口加载不出 Stripe 资源。
        实测 softbank 出口对同一条链始终空白，换出口后表单秒出。

    单席位长链（chatgpt.com/checkout/openai_llc/...）走另一条路：
    它要登录态才打得开，无痕浏览器一律被 302 到 /auth/login，
    按页面判断会把好链接误判成废链。改查服务端 session 状态，
    既准确又不用开浏览器。
    """
    from playwright.sync_api import sync_playwright

    url = (checkout_url or "").strip()
    if not url:
        return {"usable": False, "reason": "长链为空", "kind": "empty"}

    from core.elements_checkout import is_elements_checkout
    if is_elements_checkout(url):
        return _probe_elements_link(url, account=account, timeout=timeout)

    launch: dict[str, Any] = {"headless": True}
    option = build_proxy_option(proxy)
    if option:
        launch["proxy"] = option

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.set_default_timeout(int(timeout * 1000))
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                return {"usable": False, "kind": "network",
                        "reason": f"打不开页面（{type(exc).__name__}）"}
            # 表单是 React 渲染的，给它一点时间，但别等满整个支付超时
            try:
                page.wait_for_selector(SEL_FORM, timeout=int(min(timeout, 20.0) * 1000))
                return {"usable": True, "kind": "ready", "reason": "表单已就绪"}
            except Exception:
                pass
            text = ""
            try:
                text = (page.inner_text("body") or "").strip()
            except Exception:
                pass
            low = text.lower()
            if any(mark in low for mark in _EXPIRED_MARKERS):
                return {"usable": False, "kind": "expired",
                        "reason": "结账会话已过期或已完成"}
            if not text:
                return {"usable": False, "kind": "blank",
                        "reason": "页面空白，该出口加载不出 Stripe 资源"}
            return {"usable": False, "kind": "unknown",
                    "reason": f"未出现支付表单（页面显示：{text[:60]}）"}
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _probe_elements_link(url: str, *, account: dict | None = None,
                         timeout: float = 30.0) -> dict[str, Any]:
    """查单席位长链的服务端状态。

    不开浏览器：这个页面要登录态，无痕访问一律跳登录页，
    拿页面内容判断会把好链接误判成废的。直接问 checkout API：
        status=open + payment_status=unpaid  -> 还能付
        其它                                  -> 付过了或已作废
    """
    import re as _re
    import uuid as _uuid

    m = _re.search(r"(oaics_[0-9a-f]+)", url)
    if not m:
        return {"usable": False, "kind": "unknown",
                "reason": "链接里没有 checkout session id"}
    sid = m.group(1)

    if not account:
        return {"usable": False, "kind": "unknown",
                "reason": "缺少账号信息，无法查询单席位长链状态"}

    from config.proxy import normalize_proxy_url
    from core.chatgpt_plan import normalize_token, token_claims
    from core.session import BrowserSession
    from core.single_seat import CHECKOUT_DETAIL, CODEX_HEADERS

    token = normalize_token(str(account.get("access_token") or ""))
    if not token:
        return {"usable": False, "kind": "unknown", "reason": "账号没有 token"}
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {"usable": False, "kind": "unknown",
                "reason": "账号 token 已过期，请先查活"}

    proxy = str(account.get("proxy_used") or "")
    env = BrowserSession(
        proxy=normalize_proxy_url(proxy) if proxy else None,
        detect_exit_geo=False, fingerprint_seed=f"probe:{_uuid.uuid4()}")
    try:
        for _ in range(3):
            try:
                env.get("https://chatgpt.com/auth/login",
                        headers=env.get_chatgpt_navigate_headers(
                            referer="https://chatgpt.com/",
                            user_initiated=True),
                        allow_redirects=True, timeout=15)
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
        env.blocked_until = 0.0
        env.blocked_reason = ""

        h = env.get_chatgpt_headers(referer="https://chatgpt.com/")
        h["authorization"] = f"Bearer {token}"
        if claims.get("account_id"):
            h["chatgpt-account-id"] = str(claims["account_id"])
        h.update(CODEX_HEADERS)
        resp = env.get(
            CHECKOUT_DETAIL.format(sid=sid) + "?supports_business_seat_offer=true",
            headers=h, timeout=int(timeout))
        if resp.status_code != 200:
            return {"usable": False, "kind": "network",
                    "reason": f"查询长链状态 HTTP {resp.status_code}"}
        data = resp.json()
        status = str(data.get("status") or "")
        paid = str(data.get("payment_status") or "")
        state = data.get("checkout_state") or {}
        items = state.get("lineItems") or []
        seats = sum(int(i.get("quantity") or 0) for i in items)
        total = ((state.get("total") or {}).get("total") or {}).get("amount")
        if status == "open" and paid == "unpaid":
            return {"usable": True, "kind": "ready",
                    "reason": f"可支付（{seats} 席，应付 {total}）",
                    "seats": seats, "total": total}
        if paid == "paid":
            return {"usable": False, "kind": "expired",
                    "reason": "这条长链已经支付过了"}
        return {"usable": False, "kind": "expired",
                "reason": f"长链不可用（status={status} payment={paid}）"}
    except Exception as exc:  # noqa: BLE001
        return {"usable": False, "kind": "network",
                "reason": f"查询失败：{type(exc).__name__}: {exc}"}
    finally:
        try:
            env.close()
        except Exception:  # noqa: BLE001
            pass


def probe_with_fallback(checkout_url: str, *, proxy: str = "",
                        candidates: list[str] | None = None,
                        timeout: float = 30.0,
                        account: dict | None = None) -> dict[str, Any]:
    """先用本单代理探，空白就换候选出口再试。

    空白意味着长链很可能没问题，只是这个出口连不上 Stripe 的静态资源。
    会话过期则换谁都一样，不浪费时间继续探。

    单席位长链查的是服务端状态，和出口无关，所以探一次就够，
    不用再换出口重试。
    """
    from core.elements_checkout import is_elements_checkout

    first = probe_checkout_link(checkout_url, proxy=proxy, timeout=timeout,
                                account=account)
    if first.get("usable") or first.get("kind") == "expired":
        return {**first, "proxy": proxy}
    if is_elements_checkout(checkout_url):
        # 换出口对它没意义：结论来自服务端，不是页面渲染
        return {**first, "proxy": proxy}

    for alt in candidates or []:
        alt = (alt or "").strip()
        if not alt or alt == proxy:
            continue
        res = probe_checkout_link(checkout_url, proxy=alt, timeout=timeout,
                                  account=account)
        if res.get("usable"):
            logger.warning("[pay] 本单代理打不开结账页，改用备用出口 %s",
                           alt.split("@")[-1])
            return {**res, "proxy": alt, "switched": True,
                    "original_reason": first.get("reason")}
    return {**first, "proxy": proxy}


def pay_checkout(checkout_url: str, *, email: str, proxy: str = "",
                 cfg: Any = None, client: Any = None) -> dict[str, Any]:
    """打开结账页，填卡并按闸门决定是否提交。"""
    if cfg is None:
        from config import vcc as cfg  # type: ignore[no-redef]
    if not getattr(cfg, "VCC_AUTO_PAY_ENABLED", False):
        return PayResult(stage="disabled",
                         message="VCC_AUTO_PAY_ENABLED=False，未执行支付").as_dict()

    from core.vcc_client import VccError, build_client_from_config

    result = PayResult()
    out_dir = Path(cfg.VCC_SCREENSHOT_DIR or "cache/vcc_pay")

    try:
        client = client or build_client_from_config()
        result.stage = "pick_card"
        card_row = client.pick_card(card_id=(cfg.VCC_CARD_ID or "").strip(),
                                    min_balance=cfg.VCC_MIN_CARD_BALANCE)
        card_id = str(card_row.get("cardId") or "")
        result.card_mask = str(card_row.get("cardNoMask") or "")
        logger.info("[vcc-pay] 选用卡 %s（%s）", card_id, result.card_mask)

        result.stage = "billing_name"
        if (cfg.VCC_NAME_SOURCE or "holder").lower() == "config":
            name = (cfg.VCC_BILLING_NAME or "").strip()
            if not name:
                raise VccPayError("VCC_NAME_SOURCE=config 但 VCC_BILLING_NAME 为空")
        else:
            # 卡行自带 holderName，且它就是卡面姓名；持卡人表可能查不到这个
            # holderId（老卡的持卡人记录会从列表里消失），所以优先用卡自己的。
            name = str(card_row.get("holderName") or "").strip()
            if not name:
                holder = client.pick_cardholder(int(cfg.VCC_HOLDER_ID or 0))
                name = str(holder.get("holderName") or "").strip() or " ".join(
                    p for p in (str(holder.get("firstName") or "").strip(),
                                str(holder.get("lastName") or "").strip()) if p)
            if not name:
                raise VccPayError("持卡人姓名为空，请改用 VCC_BILLING_NAME")

        billing = {
            "name": name,
            "country": (cfg.VCC_BILLING_COUNTRY or "").strip().upper(),
            "state": (cfg.VCC_BILLING_STATE or "").strip(),
            "city": (cfg.VCC_BILLING_CITY or "").strip(),
            "line1": (cfg.VCC_BILLING_LINE1 or "").strip(),
            "line2": (cfg.VCC_BILLING_LINE2 or "").strip(),
            "postal": (cfg.VCC_BILLING_POSTAL or "").strip(),
        }
        for key in ("country", "city", "line1"):
            if not billing[key]:
                raise VccPayError(f"账单地址缺少必填项：{key}")

        result.stage = "card_secrets"
        card = client.card_secrets(card_id)   # 含敏感明文，不要落日志
    except VccError as exc:
        result.stage, result.message = "vcc_api", str(exc)
        logger.error("[vcc-pay] VCC 接口失败：%s", exc)
        return result.as_dict()
    except VccPayError as exc:
        result.message = str(exc)
        logger.error("[vcc-pay] %s", exc)
        return result.as_dict()

    from playwright.sync_api import sync_playwright

    # 交还窗口模式与自动提交互斥：人要自己点，就绝不能由程序先点掉。
    keep_open = bool(getattr(cfg, "VCC_KEEP_OPEN", False))
    auto_submit = bool(getattr(cfg, "VCC_AUTO_SUBMIT", False)) and not keep_open

    launch: dict[str, Any] = {
        # 交还窗口必须有头，否则人看不到也点不了
        "headless": bool(cfg.VCC_HEADLESS) and not keep_open,
    }
    channel = (getattr(cfg, "VCC_BROWSER_CHANNEL", "") or "").strip()
    exec_path = (getattr(cfg, "VCC_BROWSER_PATH", "") or "").strip()
    if channel:
        launch["channel"] = channel
    elif exec_path:
        launch["executable_path"] = exec_path
    elif not launch["headless"]:
        # Playwright 自带的是 chromium_headless_shell，开不出窗口，提前说清楚
        raise VccPayError(
            "需要有头浏览器，但未配置 VCC_BROWSER_CHANNEL / VCC_BROWSER_PATH；"
            "Playwright 自带内核为 headless shell，无法显示窗口")
    if proxy:
        launch["proxy"] = build_proxy_option(proxy)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        try:
            page = browser.new_page()
            page.set_default_timeout(int(cfg.VCC_PAGE_TIMEOUT * 1000))
            result.stage = "open"
            page.goto(checkout_url, wait_until="domcontentloaded")

            fill_checkout(page, card=card, email=email, billing=billing,
                          cfg=cfg, result=result, out_dir=out_dir)

            # 金额必须在地址填完之后读 —— 地址决定税费
            result.stage = "amount_gate"
            currency, amount = read_total(page)
            result.currency, result.amount = currency, amount
            result.expected = float(cfg.VCC_EXPECTED_AMOUNT or 0.0)
            logger.info("[vcc-pay] 页面应付 %s %s（期望 %s）",
                        currency or "?", amount, result.expected or "不校验")

            gate_error = ""
            try:
                check_amount_gate(
                    amount, currency,
                    expected=float(cfg.VCC_EXPECTED_AMOUNT or 0.0),
                    expected_currency=(cfg.VCC_EXPECTED_CURRENCY or "").strip(),
                    tolerance=float(cfg.VCC_AMOUNT_TOLERANCE or 0.0),
                    max_charge=float(cfg.VCC_MAX_CHARGE or 0.0))
            except AmountGateError as exc:
                gate_error = str(exc)
                _screenshot(page, out_dir, "amount_gate_blocked", result.screenshots)
                logger.error("[vcc-pay] %s", exc)
                # 交还窗口时闸门不通过也不关窗：人需要看到页面才能判断差异在哪
                if not keep_open:
                    result.ok, result.message = False, gate_error
                    return result.as_dict()

            if keep_open:
                return _hand_over(page, cfg, result, out_dir,
                                  currency=currency, amount=amount,
                                  gate_error=gate_error)

            if not auto_submit:
                result.ok = True
                result.message = (f"已填好并通过金额校验（{currency} {amount:.2f}），"
                                  "VCC_AUTO_SUBMIT=False 未提交")
                result.stage = "ready_not_submitted"
                logger.info("[vcc-pay] %s", result.message)
                return result.as_dict()

            result.stage = "submit"
            logger.warning("[vcc-pay] 即将真实提交支付：%s %s", currency, amount)
            page.click(SEL_SUBMIT)
            result.submitted = True

            if getattr(cfg, "VCC_3DS_ENABLED", True):
                _handle_3ds(page, client, card_id, cfg, result, out_dir)

            page.wait_for_timeout(5000)
            _screenshot(page, out_dir, "after_submit", result.screenshots)
            result.stage = "submitted"
            result.ok = True
            result.message = f"已提交支付（{currency} {amount:.2f}）"
            err = _page_error(page)
            if err:
                result.ok = False
                result.message = f"提交后页面报错：{err}"
                logger.error("[vcc-pay] %s", result.message)
            return result.as_dict()
        except Exception as exc:  # noqa: BLE001 - 汇总成结果而非上抛
            result.message = f"{type(exc).__name__}: {exc}"
            logger.exception("[vcc-pay] 支付流程异常")
            try:
                _screenshot(page, out_dir, f"error_{result.stage}", result.screenshots)
            except Exception:  # noqa: BLE001
                pass
            return result.as_dict()
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def _hand_over(page: Any, cfg: Any, result: PayResult, out_dir: Path, *,
               currency: str, amount: float | None,
               gate_error: str = "") -> dict[str, Any]:
    """表单已填好，把窗口交给人工点提交。

    这里不点任何按钮。只等页面离开结账表单（人点了提交会跳转或出现回执），
    或者等人把窗口关掉。
    """
    result.stage = "awaiting_human"
    shown = f"{currency or '?'} {amount:.2f}" if amount is not None else "未能读出"
    banner = (f"表单已填好，应付 {shown}。请自己核对后点页面上的提交按钮。"
              if not gate_error else
              f"表单已填好，但金额闸门未通过：{gate_error} 请自行判断后再决定是否提交。")
    logger.warning("[vcc-pay] %s", banner)

    try:
        page.evaluate(
            """(text) => {
                const el = document.createElement('div');
                el.textContent = text;
                el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
                    + 'background:#1a1a1a;color:#fff;padding:10px 14px;font:14px/1.5 sans-serif;'
                    + 'text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.4)';
                document.body.appendChild(el);
            }""", banner)
    except Exception as exc:  # noqa: BLE001 - 横幅只是提示，失败不影响交接
        logger.debug("[vcc-pay] 注入提示横幅失败：%s", exc)

    deadline = time.time() + float(getattr(cfg, "VCC_KEEP_OPEN_TIMEOUT", 1800.0) or 1800.0)
    closed = False
    left_form = False
    while time.time() < deadline:
        if page.is_closed():
            closed = True
            break
        try:
            if page.query_selector(SEL_SUBMIT) is None:
                left_form = True
                break
        except Exception:  # noqa: BLE001 - 窗口正在关闭时查询会抛
            closed = True
            break
        try:
            page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001
            closed = True
            break

    result.expected = float(getattr(cfg, "VCC_EXPECTED_AMOUNT", 0.0) or 0.0)
    if left_form:
        result.submitted = True
        result.ok = True
        result.stage = "human_submitted"
        result.message = f"人工已提交（页面应付 {shown}）"
        logger.info("[vcc-pay] 检测到人工提交，窗口继续保留")
        try:
            page.wait_for_timeout(3000)
        except Exception:  # noqa: BLE001
            pass
        _screenshot(page, out_dir, "after_human_submit", result.screenshots)
        err = _page_error(page)
        if err:
            result.ok = False
            result.message = f"人工提交后页面报错：{err}"
        # 提交后可能还有 3DS 要你操作，窗口留到你关掉或超时为止，别打断
        while time.time() < deadline:
            try:
                if page.is_closed():
                    break
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001
                break
    elif closed:
        result.ok = False
        result.stage = "human_closed"
        result.message = "窗口被关闭，未确认是否提交"
    else:
        result.ok = False
        result.stage = "human_timeout"
        result.message = (f"等待人工提交超时（{int(getattr(cfg, 'VCC_KEEP_OPEN_TIMEOUT', 1800.0))}s），"
                          "未检测到提交")
    logger.info("[vcc-pay] %s", result.message)
    return result.as_dict()


def _handle_3ds(page: Any, client: Any, card_id: str, cfg: Any,
                result: PayResult, out_dir: Path) -> None:
    """提交后如出现 3DS 输入框，轮询 VCC 取码填入。"""
    started = time.time()
    try:
        page.wait_for_timeout(4000)
        frames = [f for f in page.frames
                  if "challenge" in (f.url or "").lower()
                  or "3ds" in (f.name or "").lower()]
        if not frames:
            return
        logger.info("[vcc-pay] 检测到 3DS 质询，开始轮询验证码")
        _screenshot(page, out_dir, "3ds_challenge", result.screenshots)
        code = client.wait_3ds_code(
            card_id, timeout=float(cfg.VCC_3DS_TIMEOUT),
            interval=float(cfg.VCC_3DS_POLL_INTERVAL), since=started)
        if not code:
            result.message = "3DS 验证码在超时内未到达"
            logger.error("[vcc-pay] %s", result.message)
            return
        frame = frames[0]
        for selector in ('input[type="tel"]', 'input[type="text"]',
                         'input[autocomplete="one-time-code"]', "input"):
            try:
                frame.fill(selector, code)
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            result.message = "3DS 验证码已取到但未找到输入框"
            logger.error("[vcc-pay] %s", result.message)
            return
        for selector in ('button[type="submit"]', "button"):
            try:
                frame.click(selector)
                break
            except Exception:  # noqa: BLE001
                continue
        logger.info("[vcc-pay] 3DS 验证码已提交")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[vcc-pay] 3DS 处理异常：%s", exc)


#: 提交后页面明确表示"付款没成功"的文案。
#:
#: 2026-11-18 事故：#110 用 9636 付款，提交后页面跳回 "Configure your plan"
#: 并显示红框 "Payment was not approved"，但 _page_error 只认 Stripe 的 4 个
#: CSS 选择器，没命中 OpenAI 侧这个红框，于是被判成成功——paid_at 落下、
#: 账号移出候选队列，实际一分钱没扣、订阅也没建立（卡余额 20.96 前后不变、
#: subscriptions 接口 404）。假的成功比明确的失败危害大得多。
_FAILURE_TEXTS = (
    "payment was not approved",
    "payment was declined",
    "your card was declined",
    "card was declined",
    "payment failed",
    "we could not process your payment",
    "there was a problem with your payment",
    "付款未获批准",
    "支付未成功",
    "カードが拒否されました",
    "お支払いを処理できませんでした",
)


def _page_error(page: Any) -> str:
    """页面上是否显示了明确的失败信息；返回文案，没有则空串。

    先查 Stripe 的结账页错误元素，再查 OpenAI 自己渲染的失败文案
    （拒付后面会跳回套餐配置页并给出红框提示，那种情况没有 Stripe 元素）。
    """
    for selector in (".FormFieldGroup-Error", '[data-testid="error-message"]',
                     ".ConfirmPayment-PostSubmit .Text-color--red",
                     ".CheckoutPaymentForm .Text-color--red"):
        try:
            text = page.inner_text(selector, timeout=1500).strip()
        except Exception:  # noqa: BLE001
            continue
        if text:
            return text[:300]

    # OpenAI 侧的失败提示：整页文本里找关键短语
    try:
        body = (page.inner_text("body", timeout=2000) or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    for phrase in _FAILURE_TEXTS:
        if phrase in body:
            return phrase
    return ""
