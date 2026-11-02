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


def _fill(page: Any, selector: str, value: str, *, label: str,
          filled: dict[str, bool], required: bool = True) -> None:
    if not value:
        if required:
            raise VccPayError(f"{label} 为空，无法填写")
        filled[label] = False
        return
    try:
        page.fill(selector, value)
        filled[label] = True
    except Exception as exc:  # noqa: BLE001
        if required:
            raise VccPayError(f"填写 {label} 失败：{type(exc).__name__}") from exc
        logger.debug("[vcc-pay] 选填项 %s 跳过：%s", label, exc)
        filled[label] = False


def _select(page: Any, selector: str, value: str, *, label: str,
            filled: dict[str, bool], required: bool = True) -> None:
    if not value:
        filled[label] = False
        if required:
            raise VccPayError(f"{label} 为空，无法选择")
        return
    try:
        page.select_option(selector, value=value)
        filled[label] = True
    except Exception:
        try:
            page.select_option(selector, label=value)
            filled[label] = True
            return
        except Exception as exc:  # noqa: BLE001
            if required:
                raise VccPayError(
                    f"选择 {label}={value} 失败，页面可能不接受该取值") from exc
            logger.debug("[vcc-pay] 选填下拉 %s 跳过：%s", label, exc)
            filled[label] = False


def _check(page: Any, selector: str, *, label: str,
           filled: dict[str, bool], required: bool) -> None:
    try:
        if page.is_checked(selector):
            filled[label] = True
            return
        page.check(selector)
        filled[label] = True
    except Exception as exc:  # noqa: BLE001
        if required:
            raise VccPayError(f"勾选 {label} 失败：{type(exc).__name__}") from exc
        logger.debug("[vcc-pay] 选填勾选 %s 跳过：%s", label, exc)
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


def fill_checkout(page: Any, *, card: dict[str, str], email: str,
                  billing: dict[str, str], cfg: Any,
                  result: PayResult, out_dir: Path) -> None:
    """填写结账表单。不提交，不校验金额。"""
    variant = detect_variant(page)
    if variant and variant != "classic":
        raise VccPayError(
            f"结账页变体为 {variant}，当前仅适配 classic；已停止以免误填")
    result.stage = "fill"
    page.wait_for_selector(SEL_FORM, timeout=int(cfg.VCC_PAGE_TIMEOUT * 1000))

    filled = result.filled
    _fill(page, SEL_EMAIL, email, label="email", filled=filled)
    _fill(page, SEL_CARD_NUMBER, card["number"], label="card_number", filled=filled)
    _fill(page, SEL_CARD_EXPIRY, f"{card['exp_month']} / {card['exp_year']}",
          label="card_expiry", filled=filled)
    _fill(page, SEL_CARD_CVC, card["cvc"], label="card_cvc", filled=filled)
    _fill(page, SEL_BILLING_NAME, billing["name"], label="billing_name", filled=filled)

    # 国家必须先选：省份下拉按国家重新渲染，顺序反了会选不到州。
    _select(page, SEL_COUNTRY, billing["country"], label="billing_country", filled=filled)
    page.wait_for_timeout(400)

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
        actual = ""
        try:
            actual = page.input_value(SEL_STATE) or ""
        except Exception:  # noqa: BLE001
            actual = ""
        if actual.strip().upper() != want_state.strip().upper():
            raise VccPayError(
                f"账单州/省未选中（期望 {want_state}，页面实际 {actual or '空'}）。"
                "州/省会影响税费，已停止以免提交错误的账单地址")
    else:
        filled["billing_state"] = False

    _fill(page, SEL_POSTAL, billing.get("postal", ""), label="billing_postal",
          filled=filled, required=False)

    if getattr(cfg, "VCC_SAVE_PAYMENT_METHOD", False):
        _check(page, SEL_SAVE_PM, label="save_payment_method",
               filled=filled, required=False)
    # OpenAI 商务条款是必填项，不勾无法提交
    if getattr(cfg, "VCC_ACCEPT_TERMS", True):
        _check(page, SEL_TERMS, label="accept_terms", filled=filled, required=True)

    # 地址落定后页面会重算税，等它稳下来再读金额
    page.wait_for_timeout(1500)
    _screenshot(page, out_dir, "filled", result.screenshots)


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
        launch["proxy"] = {"server": proxy}

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


def _page_error(page: Any) -> str:
    for selector in (".FormFieldGroup-Error", '[data-testid="error-message"]',
                     ".ConfirmPayment-PostSubmit .Text-color--red",
                     ".CheckoutPaymentForm .Text-color--red"):
        try:
            text = page.inner_text(selector, timeout=1500).strip()
        except Exception:  # noqa: BLE001
            continue
        if text:
            return text[:300]
    return ""
