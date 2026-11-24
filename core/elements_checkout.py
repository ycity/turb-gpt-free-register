# -*- coding: utf-8 -*-
"""单席位结账页（OpenAI 自托管 Elements 变体）的填表与读数。

和 pay.openai.com 的 classic 变体是两套完全不同的页面：

    classic（2 席走的 Stripe 托管页）
        pay.openai.com/c/pay/cs_live_...
        卡号是主文档普通 input（#cardNumber），匿名可访问，带账单地址表单
    elements（1 席走的 OpenAI 自托管页）
        chatgpt.com/checkout/openai_llc/oaics_...
        卡号在 Stripe Elements iframe 里，必须先登录，没有账单地址字段

实采结构（2026-11 验证）：
    主文档   button[type=submit]            提交，文案 Subscribe
             #business-plan-standard-seats  席位数
    Elements iframe（js.stripe.com/v3/elements-inner-*）
             #payment-numberInput  name=number  autocomplete=cc-number
             #payment-expiryInput  name=expiry  MM / YY
             #payment-cvcInput     name=cvc
             #payment-savePaymentInput  保存卡片

两个坑：
  1. 页面语言跟随出口画像（实测出现过日语和英语），所以金额和按钮
     不能靠文案定位，要么用结构，要么同时匹配多语言关键词。
  2. iframe 是动态创建的，加载慢，必须等字段出现而不是等页面 load。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Elements iframe 内的字段。id 和 name 都给，id 失效时用 name 兜底。
SEL_EL_NUMBER = "#payment-numberInput, input[name='number']"
SEL_EL_EXPIRY = "#payment-expiryInput, input[name='expiry']"
SEL_EL_CVC = "#payment-cvcInput, input[name='cvc']"
SEL_EL_SAVE = "#payment-savePaymentInput, input[name='savePayment']"

# 账单地址在另一个 Stripe iframe 里（和卡号那个不是同一个）。
# 它渲染得更晚，而且页面不滚到底时可能一直不挂上来。
SEL_AD_NAME = "#billingAddress-nameInput, input[name='name']"
SEL_AD_COUNTRY = "#billingAddress-countryInput, select[name='country']"
SEL_AD_LINE1 = "#billingAddress-addressLine1Input, input[name='addressLine1']"
SEL_AD_LINE2 = "#billingAddress-addressLine2Input, input[name='addressLine2']"
SEL_AD_CITY = "#billingAddress-localityInput, input[name='locality']"
SEL_AD_STATE = ("#billingAddress-administrativeAreaInput, "
                "select[name='administrativeArea']")
SEL_AD_POSTAL = "#billingAddress-postalCodeInput, input[name='postalCode']"

# 主文档
SEL_EL_SUBMIT = "button[type='submit']"
SEL_EL_SEATS = "#business-plan-standard-seats"

# Stripe Elements 的 iframe 地址特征
_ELEMENTS_FRAME = "js.stripe.com/v3/elements-inner"

# "Due today" 的多语言写法。页面语言跟随出口，不能只认英文。
# 中文要繁简都列：实测台湾出口渲染成 zh-TW「今天應付金額」，
# 和简体「今日应付」字都不一样，只写一种会读不到金额。
_TOTAL_LABELS = (
    "due today", "today's total", "total due",
    "今日のお支払い額", "お支払い額",              # 日语
    "今天應付金額", "今天应付金额",                # 繁简：实采确认
    "今日应付", "今日付款", "应付金额", "應付金額",  # 其它中文写法
    "hoy", "aujourd'hui", "heute",                # 西/法/德
)


def is_elements_checkout(url: str) -> bool:
    """这个链接是不是单席位的自托管结账页。"""
    u = str(url or "")
    return "/checkout/openai_llc/" in u or "oaics_" in u


def _nudge_card_tab(page: Any) -> bool:
    """点一下 Card 标签，催 Stripe 把卡号输入框挂上来。

    支付方式有 Card / PayPal 两个 tab。实测 iframe 有时先出壳子、
    输入框迟迟不渲染，点一下当前 tab 能把它催出来。
    点不到就算了，返回是否点成功。
    """
    for fr in page.frames:
        u = str(getattr(fr, "url", "") or "")
        if _ELEMENTS_FRAME not in u:
            continue
        for sel in ("#card-tab", "[data-testid='card']"):
            try:
                el = fr.query_selector(sel)
                if el:
                    el.click()
                    logger.info("[elements] 点了 Card 标签催渲染")
                    return True
            except Exception:  # noqa: BLE001 - 点不到就换下一个
                continue
    return False


def find_elements_frame(page: Any, timeout_ms: int = 30000) -> Any:
    """等 Stripe Elements iframe 出现并返回它。

    iframe 是 JS 动态插的，页面 domcontentloaded 时还不存在；
    而且同源下有十几个 stripe iframe，名字都叫 elements-inner-*，
    只有真正挂了卡号输入框的那个能用——其余是 loader/accessory 之类的壳。

    所以不能"找到 iframe 就用"，必须确认里面有卡号字段；而且 Stripe
    的内部渲染比 iframe 出现晚好几秒，要一直轮询到超时。
    """
    import time as _t
    deadline = _t.time() + timeout_ms / 1000.0
    last = ""
    seen: set[str] = set()
    rounds = 0
    nudged = False
    while _t.time() < deadline:
        rounds += 1
        for fr in page.frames:
            u = str(getattr(fr, "url", "") or "")
            if _ELEMENTS_FRAME not in u:
                continue
            seen.add(u[:80])
            try:
                if fr.query_selector(SEL_EL_NUMBER):
                    logger.info("[elements] 第 %s 轮找到卡号字段：%s",
                                rounds, u[:70])
                    return fr
                last = f"候选 iframe 里还没有卡号字段：{u[:70]}"
            except Exception as exc:  # noqa: BLE001 - frame 可能正在销毁
                last = f"{type(exc).__name__}: {exc}"
        # 等了几秒还没有就催一下，别干等到超时
        if not nudged and rounds > 12 and seen:
            nudged = _nudge_card_tab(page)
        page.wait_for_timeout(500)
    logger.warning("[elements] 轮询 %s 轮仍没找到卡号字段，候选 iframe：%s",
                   rounds, sorted(seen) or "一个都没有")
    raise RuntimeError(
        f"{last or '等不到 Stripe Elements iframe'}"
        f"（轮询 {rounds} 轮 / {timeout_ms / 1000:.0f}s，"
        f"候选 {len(seen)} 个）")


def find_address_frame(page: Any, timeout_ms: int = 40000) -> Any | None:
    """找账单地址所在的 Stripe iframe。

    和卡号不在同一个 iframe：实采里卡号在 frame[5]，地址在 frame[18]，
    URL 前缀一样（elements-inner-accessory-target-*），只能靠字段区分。

    地址区块渲染得比卡号晚很多，页面不滚到底时可能一直不挂上来，
    所以这里会主动往下滚。找不到返回 None 由调用方决定怎么办。
    """
    import time as _t

    deadline = _t.time() + timeout_ms / 1000.0
    scrolled = False
    rounds = 0
    seen: set[str] = set()
    while _t.time() < deadline:
        rounds += 1
        for fr in page.frames:
            u = str(getattr(fr, "url", "") or "")
            if _ELEMENTS_FRAME not in u:
                continue
            seen.add(u[:70])
            try:
                if fr.query_selector(SEL_AD_COUNTRY):
                    logger.info("[elements] 第 %s 轮找到账单地址 iframe", rounds)
                    return fr
            except Exception:  # noqa: BLE001 - frame 可能正在销毁
                continue
        if not scrolled:
            # 地址区块在页面下方，不滚过去可能不渲染。
            # 不用 page.mouse.wheel：CloakBrowser 的 humanize 会把它变成
            # 带轨迹的真实鼠标动作，实测会长时间阻塞（卡过 5 分钟）。
            # 直接用 JS 滚，瞬间完成且不受 humanize 影响。
            try:
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                scrolled = True
                logger.info("[elements] 已滚到页面底部催地址区块渲染")
            except Exception as exc:  # noqa: BLE001
                logger.debug("[elements] 滚动失败：%s", exc)
                scrolled = True   # 失败也别每轮重试，免得拖慢轮询
        page.wait_for_timeout(600)
    logger.warning("[elements] 轮询 %s 轮没找到账单地址 iframe，候选：%s",
                   rounds, sorted(seen) or "一个都没有")
    return None


def _js_select(frame: Any, selector: str, value: str) -> str:
    """用 JS 直接设下拉值并派发事件，绕开 humanize。

    不能用 frame.select_option：CloakBrowser 的 humanize 会把它改写成
    "移鼠标 + 点开下拉 + 找选项"的真实操作，在 Stripe 这种自绘下拉上
    会无限阻塞，而且 Playwright 的 timeout 参数管不住它
    （线上 #75 卡死 5 分钟就是这么来的）。
    对照实验：裸 Playwright 下 select_option 只要 0.2s。

    React 受控组件只改 value 不会触发状态更新，必须用原生 setter
    再派发 change/input，否则页面显示变了但内部 state 没变。
    """
    return str(frame.evaluate(
        """([sel, val]) => {
          const el = document.querySelector(sel);
          if (!el) return '';
          const proto = Object.getPrototypeOf(el);
          const desc = Object.getOwnPropertyDescriptor(proto, 'value');
          if (desc && desc.set) desc.set.call(el, val);
          else el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return el.value;
        }""", [selector, value]) or "")


def _js_fill(frame: Any, selector: str, value: str) -> str:
    """用 JS 直接填输入框，绕开 humanize 的逐字输入。

    和 _js_select 同一个原因：humanize 下 frame.fill 会变成真实键盘
    输入，在 iframe 里容易卡住。地址字段没有 Stripe 的格式化逻辑，
    直接设值即可（卡号那几个字段仍要逐字输入，不能用这个）。
    """
    return str(frame.evaluate(
        """([sel, val]) => {
          const el = document.querySelector(sel);
          if (!el) return '';
          const proto = Object.getPrototypeOf(el);
          const desc = Object.getOwnPropertyDescriptor(proto, 'value');
          if (desc && desc.set) desc.set.call(el, val);
          else el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return el.value;
        }""", [selector, value]) or "")


def fill_elements_address(page: Any, billing: dict[str, str], *,
                          timeout_ms: int = 40000) -> dict[str, bool]:
    """填账单地址。

    这个页面的账单国家默认跟着 checkout session 的 billing_details 走
    （我们按 EG 建单拿 EGP 定价），但卡的真实归属地是美国，
    不改成卡的国家会被发卡行判定地址不符而拒付。

    国家必须先选：州/省下拉会按国家重建，顺序反了选不到州。
    automatic_tax_enabled=true，所以改地址会重算税，总额随之变化——
    调用方要在填完地址之后再读金额。
    """
    frame = find_address_frame(page, timeout_ms=timeout_ms)
    if frame is None:
        raise RuntimeError("找不到账单地址表单（Stripe iframe 没渲染出来）")

    filled: dict[str, bool] = {}
    country = str(billing.get("country") or "").strip().upper()
    if not country:
        raise RuntimeError("账单国家为空")

    # 1) 先选国家，等州下拉按新国家重建
    got = _js_select(frame, SEL_AD_COUNTRY, country)
    if got.strip().upper() != country:
        raise RuntimeError(
            f"选账单国家 {country} 失败，页面实际 {got or '空'}")
    filled["billing_country"] = True
    logger.info("[elements] 账单国家选为 %s", country)
    frame.wait_for_timeout(1500)

    # 2) 姓名
    name = str(billing.get("name") or "").strip()
    if name:
        got = _js_fill(frame, SEL_AD_NAME, name)
        if got:
            filled["billing_name"] = True
        else:
            logger.warning("[elements] 填姓名没生效，继续（姓名不至于拒付）")

    # 3) 地址行、城市、邮编
    for sel, key, required in (
        (SEL_AD_LINE1, "line1", True),
        (SEL_AD_LINE2, "line2", False),
        (SEL_AD_CITY, "city", True),
        (SEL_AD_POSTAL, "postal", False),
    ):
        val = str(billing.get(key) or "").strip()
        if not val:
            if required:
                raise RuntimeError(f"账单地址缺少必填项：{key}")
            continue
        got = _js_fill(frame, sel, val)
        if got.strip() == val:
            filled[f"billing_{key}"] = True
        elif required:
            raise RuntimeError(
                f"填 {key} 后回读不符：期望 {val!r}，实际 {got!r}")
        else:
            logger.warning("[elements] 填 %s 回读不符：%r", key, got)

    # 4) 州/省：直接影响税额，给了值就必须选中
    state = str(billing.get("state") or "").strip()
    if state:
        # 配置里写 AK 或 Alaska 都要能选中，所以在 options 里按
        # value 和文本两头匹配，匹配到哪个用哪个的 value
        got = str(frame.evaluate(
            """([sel, want]) => {
              const el = document.querySelector(sel);
              if (!el) return '';
              const w = String(want).trim().toLowerCase();
              let hit = null;
              for (const o of el.options) {
                if (String(o.value).trim().toLowerCase() === w ||
                    String(o.text).trim().toLowerCase() === w) {
                  hit = o.value;
                  break;
                }
              }
              if (hit === null) return '';
              const proto = Object.getPrototypeOf(el);
              const desc = Object.getOwnPropertyDescriptor(proto, 'value');
              if (desc && desc.set) desc.set.call(el, hit);
              else el.value = hit;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              return el.value;
            }""", [SEL_AD_STATE, state]) or "")
        if not got:
            raise RuntimeError(
                f"账单州/省 {state} 没选中（下拉里找不到这个选项）。"
                "州直接决定税额，已停止以免提交错误的账单地址")
        filled["billing_state"] = True
        logger.info("[elements] 账单州/省选为 %s", got)

    # 税按地址重算，等它稳定再让调用方读金额
    frame.wait_for_timeout(2500)
    logger.info("[elements] 账单地址已填：%s/%s/%s",
                country, state or "-", billing.get("city") or "-")
    return filled


def _fill_frame_field(frame: Any, selector: str, value: str, *,
                      label: str, timeout_ms: int = 15000,
                      attempts: int = 3) -> str:
    """在 iframe 里填一个字段并回读，不对就重填。

    Stripe Elements 边输入边格式化（插空格、补斜杠），还会异步重排光标。
    实测 fill("") 清不干净：有效期填 "1028" 回读成 "10 / 29"——
    中间那位被格式化逻辑吃掉又补了一位。

    所以每次先全选删除，逐字输入后立刻回读；不符就整个重来。
    """
    el = frame.wait_for_selector(selector, timeout=timeout_ms, state="visible")
    want = _digits(value)
    got = ""
    for i in range(1, int(attempts) + 1):
        el.click()
        # 全选 + 删除，比 fill("") 可靠：fill 对受控组件可能不触发清空
        try:
            el.press("Control+a")
            el.press("Delete")
        except Exception:  # noqa: BLE001 - 退回 fill
            try:
                el.fill("")
            except Exception:  # noqa: BLE001
                pass
        frame.wait_for_timeout(120)
        # 慢一点输，给格式化逻辑留出处理时间
        el.type(value, delay=120)
        frame.wait_for_timeout(260)
        try:
            got = str(el.input_value() or "")
        except Exception:  # noqa: BLE001
            got = ""
        if _digits(got) == want:
            if i > 1:
                logger.info("[elements] %s 第 %s 次填对", label, i)
            return got
        logger.warning("[elements] %s 第 %s/%s 次回读不符：期望 %s 实际 %r",
                       label, i, attempts, want, got)
    return got


def _digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))


def fill_elements_card(page: Any, *, card: dict[str, str],
                       timeout_ms: int = 30000) -> dict[str, bool]:
    """在 Elements iframe 里填卡号、有效期、CVC，并逐项回读校验。

    有效期用两位年：Stripe 的 MM / YY 框只收两位，传四位会被截断成
    错误值（实测 "03 / 2030" 回读成 "0320"）。
    """
    frame = find_elements_frame(page, timeout_ms=timeout_ms)
    logger.info("[elements] 找到 Stripe Elements iframe")
    filled: dict[str, bool] = {}

    number = _digits(card.get("number", ""))
    got = _fill_frame_field(frame, SEL_EL_NUMBER, number, label="card_number")
    if _digits(got) != number:
        raise RuntimeError(
            f"卡号回读不符：期望 {len(number)} 位，实际读到 "
            f"{len(_digits(got))} 位")
    filled["card_number"] = True

    mm = str(card.get("exp_month", "")).zfill(2)[-2:]
    yy = str(card.get("exp_year", ""))[-2:]
    got = _fill_frame_field(frame, SEL_EL_EXPIRY, f"{mm}{yy}",
                            label="card_expiry")
    if _digits(got) != f"{mm}{yy}":
        raise RuntimeError(
            f"有效期回读不符：期望 {mm}/{yy}，页面实际 {got!r}")
    filled["card_expiry"] = True

    cvc = _digits(card.get("cvc", ""))
    got = _fill_frame_field(frame, SEL_EL_CVC, cvc, label="card_cvc")
    if _digits(got) != cvc:
        raise RuntimeError(f"CVC 回读不符：期望 {len(cvc)} 位，实际 {got!r}")
    filled["card_cvc"] = True

    logger.info("[elements] 卡片三要素已填好并回读校验通过")
    return filled


def read_elements_total(page: Any, *,
                        fallback_currency: str = "") -> tuple[str, float | None]:
    """读"今日应付"金额。

    页面没有 data-testid，只能靠标签文案定位；而语言跟随出口，
    所以多语言关键词都要认。找不到就返回 (currency, None)，
    由调用方按"读不到金额"处理，绝不猜一个数字出来。

    fallback_currency：页面上只有裸 "$"（实采确认 HTML 里没有币种码，
    __NEXT_DATA__ 也不存在），而 $ 可能是 USD/CAD/AUD。金额闸门在
    VCC_EXPECTED_CURRENCY 非空时要求必须识别出币种，否则直接拦住提交。
    所以把创建 checkout 时用的 billing_details.currency 传进来兜底——
    那是权威值，不是猜的。
    """
    js = """(labels) => {
      const out = [];
      document.querySelectorAll('*').forEach(el => {
        if (el.children.length > 4) return;
        const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        if (!t || t.length > 60) return;
        const low = t.toLowerCase();
        if (!labels.some(l => low.includes(l))) return;
        out.push(t);
      });
      return out;
    }"""
    try:
        cands = page.evaluate(js, list(_TOTAL_LABELS))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[elements] 读金额失败：%s", exc)
        return "", None

    from core.vcc_checkout_pay import parse_amount
    for text in cands:
        cur, amount = parse_amount(text)
        if amount is not None:
            if not cur and fallback_currency:
                want = fallback_currency.strip().upper()
                # 兜底不是无条件的：先确认页面符号和期望币种对得上，
                # 否则等于把币种校验架空了。符号对不上宁可返回空币种，
                # 让闸门按"无法识别币种"拦住。
                if _symbol_matches(text, want):
                    cur = want
                    logger.info("[elements] 页面只有货币符号，按下单币种记作 %s",
                                cur)
                else:
                    logger.warning(
                        "[elements] 页面符号与期望币种 %s 不符：%r，不做兜底",
                        want, text[:40])
            logger.info("[elements] 读到应付金额：%s %s（来自 %r）",
                        cur or "?", amount, text[:40])
            return cur, amount

    # 关键词没命中（页面语言没见过）时的兜底：按版面结构找。
    # 订单摘要里最后一个货币金额就是"今日应付"，这个不依赖语言。
    cur, amount = _read_total_by_structure(page, fallback_currency)
    if amount is not None:
        return cur, amount

    logger.warning("[elements] 没能从页面读到应付金额，候选=%s", cands[:3])
    return "", None


def _read_total_by_structure(page: Any,
                             fallback_currency: str = "") -> tuple[str, float | None]:
    """不靠文案、只靠版面找"今日应付"。

    关键词表总会漏掉没见过的语言（实测繁体「今天應付金額」就漏了），
    所以留一条不依赖语言的路：订单摘要里金额按
    小计 -> 税 -> 今日应付 的顺序排列，取最后一个即可。

    只在关键词全不命中时用，拿不准就返回 None 让闸门拦住。
    """
    from core.vcc_checkout_pay import parse_amount
    try:
        texts = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('*').forEach(el => {
            if (el.children.length) return;
            const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!t || t.length > 24) return;
            // 必须是纯金额，排除 "EGP 1,150/月" 这类单价
            if (!/^-?\\s*(?:[A-Z]{3}|[$€£¥₺₹])\\s*[\\d,.]+$/.test(t)) return;
            out.push(t);
          });
          return out;
        }""")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[elements] 结构化读金额失败：%s", exc)
        return "", None

    # 负数是折扣行，不可能是应付额
    cands = [t for t in texts if not t.lstrip().startswith("-")]
    if not cands:
        return "", None
    text = cands[-1]
    cur, amount = parse_amount(text)
    if amount is None:
        return "", None
    if not cur and fallback_currency:
        want = fallback_currency.strip().upper()
        if _symbol_matches(text, want):
            cur = want
    logger.warning("[elements] 关键词没命中，按版面结构取最后一个金额：%s %s"
                   "（来自 %r，共 %s 个候选）",
                   cur or "?", amount, text, len(cands))
    return cur, amount


# 币种码 → 该币种在页面上可能出现的符号。只列我们实际会用到的。
_CURRENCY_SYMBOLS = {
    "USD": ("$", "US$"),
    "CAD": ("$", "CA$", "C$"),
    "AUD": ("$", "A$"),
    "SGD": ("$", "S$"),
    "HKD": ("$", "HK$"),
    "TWD": ("$", "NT$"),
    "EGP": ("E£", "£", "EGP", "ج.م"),
    "EUR": ("€",),
    "GBP": ("£",),
    "JPY": ("¥", "￥"),
    "TRY": ("₺",),
    "INR": ("₹",),
    "BRL": ("R$",),
    "KRW": ("₩",),
}


def _symbol_matches(text: str, currency: str) -> bool:
    """页面金额文本里的货币符号是否可能属于这个币种。

    只做"排除明显不符"，不追求精确：$ 同时属于 USD/CAD/AUD 等，
    这里认的是"不是 € 也不是 ¥"，足以挡住币种被整体换掉的情况。
    """
    syms = _CURRENCY_SYMBOLS.get(currency.strip().upper())
    if not syms:
        # 没登记的币种不敢断言，交给调用方按读不到币种处理
        return False
    return any(s in text for s in syms)


def read_seat_quantity(page: Any) -> int | None:
    """读页面上的席位数，用于下单前核对确实是 1 席。"""
    try:
        el = page.query_selector(SEL_EL_SEATS)
        if el:
            v = el.input_value()
            return int(str(v).strip())
    except Exception:  # noqa: BLE001
        pass
    try:
        txt = page.inner_text("body", timeout=4000)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"(\d+)\s*standard seat", txt, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"スタンダードシート\s*(\d+)\s*席", txt)
    if m:
        return int(m.group(1))
    return None
