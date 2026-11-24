# -*- coding: utf-8 -*-
"""OpenAI 管理后台（admin.openai.com）账单页自动化：添加/删除支付方式。

2026-11-19 实测（#128 日语界面）探明的页面结构：

  SSO 状态机（cookie 注入后仍可能出现）：
    auth.openai.com/choose-an-account  → 点账号邮箱
    auth.openai.com/log-in/password    → 填密码提交
    auth.openai.com/mfa-challenge      → 填 TOTP 提交
    admin.openai.com/...               → 完成

  账单页 /workspace/{ws}/billing?workspace=...：
    支付方式区块按钮「新しく追加」(ja)/「Add new」(en)/「添加」(zh)
    加卡表单 = 单席位结账页同款 Stripe Elements iframe：
      #payment-numberInput / #payment-expiryInput / #payment-cvcInput
      #billingAddress-nameInput / -countryInput / -postalCodeInput /
      -administrativeAreaInput / -localityInput / -addressLine1Input /
      -addressLine2Input
      （core/elements_checkout.py 的填卡/填地址函数直接复用）
    提交时 Stripe 内嵌 invisible hCaptcha（sitekey 在
    b.stripecdn.com HCaptchaInvisible.html?id=<uuid>）；风控触发时升级为
    交互挑战，用 2Captcha 解。
    每张卡一行（品牌 + •••• 尾号 + 默认徽标），行旁的操作菜单按钮
    aria-label 含「お支払い方法のアクション」(ja)/"payment method
    actions"(en)，菜单里有 设为默认/删除。
"""
from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

# 页面文案的多语言匹配表（账单页语言跟随账号设置，ja/en/zh 都可能出现）。
T_ADD_NEW = ("新しく追加", "Add new", "Add a payment method", "Add payment method", "添加", "新增")
T_DEFAULT = ("デフォルト", "Default", "默认")
T_MAKE_DEFAULT = ("デフォルトに設定", "デフォルトにする", "Make default",
                  "Set as default", "设为默认", "设为默认付款方式")
T_REMOVE = ("削除", "Remove", "删除", "移除")
T_CONFIRM_REMOVE = ("削除", "Remove", "删除", "确认", "Confirm", "确定")
T_SAVE = ("追加", "保存", "Add", "Save", "添加")
T_CANCEL = ("キャンセル", "Cancel", "取消")
# 支付方式区块标题（判断区块渲染完成）。
T_PAY_SECTION = ("お支払い方法", "Payment methods", "Payment method", "支付方式", "付款方式")

_LAST4_RE = re.compile(r"[•\*]{2,}\s?(\d{4})")
_TIMEOUT_MS = 60000


def _log_prefix(email: str) -> str:
    return f"[换绑] {email}"


def _page_text(page) -> str:
    try:
        return page.evaluate("document.body ? document.body.innerText : ''") or ""
    except Exception:  # noqa: BLE001
        return ""


def _click_by_texts(page, texts, *, timeout_ms: int = 8000) -> bool:
    """按多语言文案点击第一个可见且可点的按钮/元素。"""
    for text in texts:
        try:
            el = page.locator(f"button:has-text('{text}')").first
            if el.count() == 0:
                continue
            if not el.is_visible():
                continue
            el.scroll_into_view_if_needed()
            el.click(timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def sso_into_admin(page, email: str, *, password: str = "", totp_code=None,
                   target_url: str, max_rounds: int = 12) -> str:
    """打开目标 URL 并穿越 SSO 状态机，返回最终 URL。

    totp_code 为 callable（每次调用取当前码）或 None。
    """
    page.goto(target_url, wait_until="commit", timeout=_TIMEOUT_MS)
    page.wait_for_timeout(8000)
    for rnd in range(1, int(max_rounds) + 1):
        url = str(page.url)
        if "admin.openai.com" in url:
            logger.info("[换绑] %s SSO 完成（%s 轮）：%s", email, rnd, url[:100])
            return url
        try:
            if "choose-an-account" in url:
                page.locator(f"text={email}").first.click(timeout=8000)
                logger.info("[换绑] %s SSO：已点账号选择", email)
            elif "/log-in/password" in url:
                if not password:
                    raise RuntimeError("SSO 要求输入密码，但账号没有可用密码")
                page.locator("input[type='password']").first.fill(password)
                page.locator("button[type='submit']").first.click()
                logger.info("[换绑] %s SSO：已填密码提交", email)
            elif "mfa" in url.lower():
                if totp_code is None:
                    raise RuntimeError("SSO 要求 MFA，但账号没有 TOTP")
                code = totp_code() if callable(totp_code) else totp_code
                page.locator(
                    "input[inputmode='numeric'], input[name='code'], input[type='text']"
                ).first.fill(str(code))
                page.locator("button[type='submit']").first.click()
                logger.info("[换绑] %s SSO：已填 TOTP", email)
            else:
                logger.debug("[换绑] %s SSO 等待中：%s", email, url[:90])
        except Exception as exc:  # noqa: BLE001 - 部分轮次点击失败重试下一轮
            logger.warning("[换绑] %s SSO 第 %s 轮处理异常：%s", email, rnd, str(exc)[:120])
        page.wait_for_timeout(8000)
    raise RuntimeError(f"SSO 多轮后仍未进入管理后台，当前落点：{str(page.url)[:160]}")


def wait_billing_section(page, *, max_rounds: int = 10) -> None:
    """等账单页支付方式区块渲染（SPA 慢加载）。"""
    for _ in range(int(max_rounds)):
        text = _page_text(page)
        if any(t in text for t in T_PAY_SECTION):
            return
        page.wait_for_timeout(6000)
    raise RuntimeError("账单页支付方式区块一直没渲染出来")


def read_payment_methods(page) -> list[dict]:
    """读取当前支付方式列表：[{brand, last4, default}]。

    解析正文文本：每张卡一段「品牌 … •••• 尾号 … [默认标记]」。
    文本顺序即页面顺序；默认徽标紧跟尾号行（只看后 2 行，
    避免相邻卡的徽标串行）。实测（2026-11-19 #128 日语页）：
        Mastercard\n•••• 5762\nデフォルト
    """
    text = _page_text(page)
    cards: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        m = _LAST4_RE.search(ln)
        if not m:
            continue
        # 品牌取尾号行上方最近的非徽标短行（Visa/Mastercard/Amex 等）。
        brand = ""
        for back in range(i - 1, max(-1, i - 4), -1):
            cand = lines[back]
            if len(cand) <= 20 and not any(t in cand for t in T_DEFAULT):
                if not _LAST4_RE.search(cand):
                    brand = cand
                    break
        # 默认标记：只看尾号行的后 2 行（徽标紧贴尾号）。
        window = " ".join(lines[i + 1:i + 3])
        is_default = any(t in window for t in T_DEFAULT)
        cards.append({"brand": brand, "last4": m.group(1), "default": is_default})
    return cards


def open_add_card_form(page) -> None:
    """点「添加支付方式」展开 Stripe Elements 表单并等 iframe 就绪。

    账单页刚渲染完就点可能点了个空（页面重渲染吞掉点击），表单没弹
    出来；间隔几秒重点最多 3 次。
    """
    from core.elements_checkout import find_elements_frame
    last_err = ""
    for attempt in range(1, 4):
        if not _click_by_texts(page, T_ADD_NEW):
            last_err = "没找到「添加支付方式」按钮（页面语言可能未覆盖）"
        else:
            try:
                find_elements_frame(page, timeout_ms=_TIMEOUT_MS)
                logger.info("[换绑] 加卡表单已展开（Stripe Elements iframe 就绪，第 %s 次点击）", attempt)
                return
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:160]
        logger.warning("[换绑] 第 %s 次点加卡未弹表单：%s", attempt, last_err)
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(4000)
    raise RuntimeError(f"加卡表单未能展开：{last_err}")


def fill_add_card_form(page, *, card: dict, billing: dict) -> None:
    """填卡号/有效期/CVC + 账单地址（复用 elements_checkout 的 iframe 填写）。"""
    from core.elements_checkout import fill_elements_card, fill_elements_address
    ok = fill_elements_card(page, card=card, timeout_ms=_TIMEOUT_MS)
    bad = [k for k, v in (ok or {}).items() if not v]
    if bad:
        raise RuntimeError(f"卡信息填写未确认：{bad}")
    ok = fill_elements_address(page, billing, timeout_ms=_TIMEOUT_MS)
    bad = [k for k, v in (ok or {}).items() if not v]
    if bad:
        raise RuntimeError(f"账单地址填写未确认：{bad}")


def _extract_hcaptcha_sitekey(page) -> tuple[str, str]:
    """从 Stripe 的 hCaptcha iframe URL 提取 sitekey，返回 (sitekey, pageurl)。"""
    pageurl = str(page.url)
    for f in page.frames:
        u = f.url or ""
        m = re.search(r"sitekey=([0-9a-f-]{30,})", u)
        if m:
            return m.group(1), pageurl
        m = re.search(r"HCaptchaInvisible\.html\?id=([0-9a-f-]{30,})", u, re.I)
        if m:
            return m.group(1), pageurl
    return "", pageurl


def solve_and_resubmit(page, *, email: str, solve_captcha) -> bool:
    """提交卡列表迟迟不变且存在挑战帧时：打码 → 注入 → 重新提交。

    solve_captcha(sitekey, pageurl) -> token | None；返回是否完成重提交。
    """
    sitekey, pageurl = _extract_hcaptcha_sitekey(page)
    if not sitekey:
        logger.warning("[换绑] %s 没提取到 hCaptcha sitekey，无法自动打码", email)
        return False
    token = ""
    try:
        token = solve_captcha(sitekey, pageurl) or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("[换绑] %s 打码失败：%s", email, str(exc)[:160])
    if not token:
        return False
    _inject_captcha_token(page, token)
    click_add_submit(page)
    page.wait_for_timeout(5000)
    return True


def has_hcaptcha_challenge(page) -> bool:
    """是否存在 hCaptcha 挑战帧（注意：Stripe 会预载，出现≠阻塞，
    只有提交后卡列表迟迟不变时才当它阻塞处理）。"""
    for f in page.frames:
        u = f.url or ""
        if "newassets.hcaptcha.com" in u and "frame=challenge" in u:
            return True
    return False


def click_add_submit(page) -> None:
    """点加卡表单的提交/保存按钮。"""
    if _click_by_texts(page, T_SAVE):
        return
    try:
        page.locator("button[type='submit']:visible").first.click(timeout=8000)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"没找到加卡提交按钮：{exc}") from exc


def _inject_captcha_token(page, token: str) -> None:
    """把 2Captcha token 注入页面（复用 pay-session 的注入逻辑思路）。"""
    script = (
        "(t) => {"
        "  const areas = [document, ...document.querySelectorAll('iframe')];"
        "  for (const d of areas) { try {"
        "    for (const id of ['g-recaptcha-response', 'h-captcha-response']) {"
        "      let el = d.getElementById ? d.getElementById(id) : null;"
        "      if (!el) { el = d.createElement ? d.createElement('textarea') : null;"
        "        if (el) { el.id = id; el.style.display = 'none';"
        "          (d.body || d.documentElement).appendChild(el); } }"
        "      if (el) el.value = t;"
        "    }"
        "    if (d.defaultView && d.defaultView.hcaptcha && d.defaultView.hcaptcha.setResponse)"
        "      d.defaultView.hcaptcha.setResponse(t);"
        "  } catch (e) {} }"
        "}"
    )
    try:
        page.evaluate(script, token)
    except Exception:  # noqa: BLE001
        logger.warning("[换绑] token 注入异常", exc_info=True)


def open_card_menu(page, last4: str) -> bool:
    """打开指定尾号那行卡的操作菜单（aria 含 payment method actions）。

    操作按钮与卡行按 DOM 顺序一一对应（实测 2026-11-19：两张卡两个
    aria 按钮，顺序=卡顺序）；先按顺序配对，失败退回按坐标就近。
    aria 匹配必须大小写不敏感：英文页是 "Payment method actions"
    （#11 实测大写 P，CSS 子串匹配默认区分大小写会漏）。
    """
    aria_texts = ("お支払い方法のアクション", "payment method actions",
                  "card actions", "more options")
    cards = read_payment_methods(page)
    idx = next((i for i, c in enumerate(cards) if c["last4"] == last4), -1)
    for aria in aria_texts:
        try:
            btns = page.locator(f"[aria-label*='{aria}' i]")
            n = btns.count()
            if n == 0:
                continue
            if idx >= 0 and idx < n:
                target = btns.nth(idx)
            else:
                target = _button_near_last4(page, btns, last4) or btns.first
            target.scroll_into_view_if_needed()
            target.click(timeout=8000)
            # radix 菜单渲染需要一点时间
            try:
                page.wait_for_selector("[role='menuitem']", timeout=5000)
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(1500)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _button_near_last4(page, btns, last4: str):
    """在 DOM 里找离「•••• last4」文本最近的操作按钮。"""
    try:
        handles = btns.all()
        cards = page.locator(f"text=•••• {last4}")
        if cards.count() == 0 or len(handles) == 0:
            return None
        card_box = cards.first.bounding_box()
        if not card_box:
            return None
        best, best_d = None, 1e18
        for h in handles:
            try:
                box = h.bounding_box()
            except Exception:  # noqa: BLE001
                continue
            if not box:
                continue
            d = abs(box["y"] - card_box["y"]) + abs(box["x"] - card_box["x"])
            if d < best_d:
                best, best_d = h, d
        return best
    except Exception:  # noqa: BLE001
        return None


def menu_action(page, texts) -> bool:
    """在已打开的卡操作菜单里点指定项（radix 菜单渲染在 portal 里）。

    实测菜单项（日语页）：デフォルトに設定 / 編集 / 削除，均为
    [role=menuitem]。先精确匹配 role，再退回任意可见文本元素。
    """
    for text in texts:
        try:
            el = page.locator(f"[role='menuitem']:has-text('{text}')").first
            if el.count() > 0 and el.is_visible():
                el.click(timeout=8000)
                return True
        except Exception:  # noqa: BLE001
            pass
    for text in texts:
        try:
            el = page.get_by_role("menuitem", name=text).first
            if el.count() > 0:
                el.click(timeout=8000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def remove_card(page, last4: str, *, email: str) -> None:
    """删除指定尾号的卡：开操作菜单 → 删除 → 确认弹窗（若有）。

    设默认/加卡后页面会刷新并弹 toast，第一次点菜单可能被遮挡或
    点到旧 DOM；失败先 Esc 关菜单、等重渲染，再试一次。
    """
    last_err = ""
    for attempt in range(1, 3):
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(3000 if attempt > 1 else 800)
        if not open_card_menu(page, last4):
            last_err = f"没找到尾号 {last4} 的卡操作菜单"
            logger.warning("[换绑] %s 第 %s 次开菜单失败：%s", email, attempt, last_err)
            continue
        if menu_action(page, T_REMOVE):
            page.wait_for_timeout(2000)
            # 删除默认卡/有订阅的卡时可能出现确认弹窗。
            if _click_by_texts(page, T_CONFIRM_REMOVE[:4]):
                logger.info("[换绑] %s 已确认删除确认弹窗（%s）", email, last4)
            page.wait_for_timeout(4000)
            return
        # 诊断：把当前菜单项 dump 出来便于定位文案差异
        try:
            items = page.evaluate(
                "() => Array.from(document.querySelectorAll('[role=menuitem]'))"
                ".map(e => (e.innerText || '').trim()).filter(Boolean)")
        except Exception:  # noqa: BLE001
            items = []
        last_err = f"菜单项里没有「删除」（实际菜单项={items}）"
        logger.warning("[换绑] %s 第 %s 次未命中删除项：%s", email, attempt, last_err)
        try:
            page.screenshot(path=f"/tmp/rebind_menu_miss_{attempt}.png")
        except Exception:  # noqa: BLE001
            pass
    raise RuntimeError(f"删除卡失败（尾号 {last4}）：{last_err}")


def set_default_card(page, last4: str, *, email: str) -> None:
    """把指定尾号的卡设为默认支付方式（带重试，同 remove_card）。"""
    last_err = ""
    for attempt in range(1, 3):
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(3000 if attempt > 1 else 800)
        if not open_card_menu(page, last4):
            last_err = f"没找到尾号 {last4} 的卡操作菜单"
            logger.warning("[换绑] %s 设默认第 %s 次开菜单失败：%s", email, attempt, last_err)
            continue
        if menu_action(page, T_MAKE_DEFAULT):
            page.wait_for_timeout(3000)
            logger.info("[换绑] %s 已把 %s 设为默认", email, last4)
            return
        last_err = "菜单里没有「设为默认」项（该卡可能已是默认）"
        logger.warning("[换绑] %s 设默认第 %s 次未命中菜单项", email, attempt)
    # 已是默认时菜单不出现该项：回读确认一次，确认了就不算失败。
    cards = read_payment_methods(page)
    if any(c["last4"] == last4 and c["default"] for c in cards):
        logger.info("[换绑] %s 回读 %s 已是默认，视为成功", email, last4)
        return
    raise RuntimeError(f"设默认失败（尾号 {last4}）：{last_err}")


def wait_card_list_change(page, before: list[dict], *, want_last4: str | None = None,
                          max_rounds: int = 12) -> list[dict]:
    """等卡列表发生变化（或出现 want_last4），返回最新列表。"""
    for _ in range(int(max_rounds)):
        page.wait_for_timeout(5000)
        now = read_payment_methods(page)
        if want_last4 and any(c["last4"] == want_last4 for c in now):
            return now
        if not want_last4 and now != before:
            return now
    return read_payment_methods(page)


def billing_url(workspace_id: str, workspace_name: str = "myteam") -> str:
    name = (workspace_name or "myteam").strip() or "myteam"
    return f"https://admin.openai.com/workspace/{workspace_id}/billing?workspace={name}"


__all__ = [
    "sso_into_admin",
    "wait_billing_section",
    "read_payment_methods",
    "open_add_card_form",
    "fill_add_card_form",
    "submit_add_card",
    "remove_card",
    "set_default_card",
    "wait_card_list_change",
    "billing_url",
]
