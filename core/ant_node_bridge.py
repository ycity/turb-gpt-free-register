# -*- coding: utf-8 -*-
"""Ant Browser 节点桥接（生产侧）。

架构：生产服务器是大脑（账号/代理/OTP 邮件/协议实现），HK Windows 节点是手
（Ant Browser 指纹内核 + CDP）。本模块把两者接起来：

- `ant_login_cookies`：节点浏览器跑完整邮箱登录（CF 指纹关卡最致命的一步），
  导出 cookies；协议侧 `session_with_cookies` 用同一代理注入 cookies，
  之后全部复用现有协议代码（reauth / OTP 校验 / 提交）。
- `run_password_backfill_hybrid`：补密码混合流程（浏览器登录 + 协议收尾）。
- `ant_rebind_page`：换卡流程的浏览器替换（page 从 CloakBrowser 换成节点）。

为什么这样切：CF 指纹级 403 集中在「登录/authorize」这类带质询的页面请求；
拿到真实浏览器下发的 CF cookie 后，同代理 IP 的纯 API 请求（sentinel/密码提交/
MFA）通常可以过。灰度阶段如仍被拦，再把对应步骤下沉到浏览器页面驱动
（core/ant_sensitive_ops.py 已有页面版实现）。

并发注意：节点统一 CDP 入口同一时刻只反代「活动实例」，因此本模块用
进程级锁串行化所有 ant 流程；灰度阶段请保持单 worker。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager

from config import antbrowser as _ant_cfg
from core.ant_browser_client import AntBrowserClient

logger = logging.getLogger(__name__)

# 节点统一 CDP 入口一次只能服务一个活动实例：所有 ant 流程串行
_NODE_FLOW_LOCK = threading.RLock()

_LOGIN_TIMEOUT = 420  # 登录页整链超时（秒）：多轮验证码 + 两轮邮件等待


def _driver_mode() -> str:
    return str(getattr(_ant_cfg, "SENSITIVE_OPS_DRIVER", "protocol") or "protocol").strip().lower()


def driver_is_ant() -> bool:
    return _driver_mode() == "ant"


def pick_tested_proxies(count: int = 3) -> list[str]:
    """按代理池测活结果挑最近可用的代理（延迟升序）。

    代理池里有死/慢节点：盲选会撞上中途断流的代理（浏览器 7 分钟长会话
    对代理稳定性要求比协议单请求高得多）。这里直接读 webui 的测活缓存
    （proxy_pool_tests：proxy_key → ok/latency_ms），只返回 ok=True 的。
    """
    import hashlib
    import sqlite3
    from contextlib import closing

    from config.proxy import PROXY_POOL

    def _identity(url: str) -> str:
        try:
            from webui.proxy_pool import _identity

            return _identity(url)
        except Exception:
            return url.split("#")[0].strip()

    def key_of(url: str) -> str:
        return hashlib.sha256(_identity(url).encode("utf-8")).hexdigest()[:24]

    candidates: list[str] = []
    for line in PROXY_POOL or []:
        text = str(line or "").strip()
        if not text or text.startswith("#"):
            continue
        url = text.split("#")[0].strip()
        if url.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://")):
            candidates.append(url)
    if not candidates:
        return []

    try:
        from core.db import DB_PATH  # noqa: F401
    except Exception:
        pass
    db_file = None
    try:
        from config.env_loader import _PROJECT_ROOT

        db_file = str(_PROJECT_ROOT / "turb.sqlite3")
    except Exception:
        db_file = "/app/turb.sqlite3"

    results: dict[str, dict] = {}
    try:
        with closing(sqlite3.connect(db_file, timeout=10)) as conn:
            for key, payload in conn.execute("SELECT proxy_key, result_json FROM proxy_pool_tests"):
                try:
                    results[key] = json.loads(payload)
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("[Ant桥] 读代理测活结果失败（盲选降级）：%s", exc)
        return candidates[:count]

    def latency_of(url: str):
        res = results.get(key_of(url)) or {}
        if res.get("ok") is not True:
            return None
        lat = res.get("latency_ms")
        return float(lat) if isinstance(lat, (int, float)) else 9_999.0

    good = [(latency_of(u), u) for u in candidates]
    good = [(lat, u) for lat, u in good if lat is not None]
    good.sort(key=lambda x: x[0])
    return [u for _, u in good[:count]]


def registration_proxy_for(email: str) -> str | None:
    """按邮箱回查账号的注册代理（proxy_used）。一号一代理是风控红线。"""
    email = str(email or "").strip().lower()
    if not email:
        return None
    try:
        from core import db

        acc = db.get_account_by_email(email) or {}
        proxy = str(acc.get("proxy_used") or "").strip()
        return proxy or None
    except Exception as exc:
        logger.warning("[Ant桥] 回查 %s 注册代理失败：%s", email, exc)
        return None


def default_otp_waiter(email: str, email_source: str | None = None, *, max_wait: int = 120):
    """登录 OTP 等待器：复用统一取码分发（core.email_provider.wait_for_otp）。"""
    from core.email_provider import wait_for_otp

    max_wait = max(30, int(max_wait or 120))

    def _wait(after_ts: float) -> str:
        return wait_for_otp(
            email,
            after_ts=after_ts,
            max_wait=max_wait,
            email_source=email_source,
            force_service=True,
        )

    return _wait


@contextmanager
def _ant_page(proxy_url: str | None, email: str | None = None):
    """打开节点实例 + Playwright 连接，yield (page, context, opened)。

    退出时关闭页面并清理本轮创建的实例。串行锁保证统一 CDP 入口不互踩。
    proxy_url 为空时从代理池取一个：节点裸 IP 被 OpenAI 边缘封禁，直连必挂；
    且浏览器与后续协议请求必须同 IP（CF cookie 连续性）。
    """
    from playwright.sync_api import sync_playwright

    proxy_url = str(proxy_url or "").strip() or None
    if not proxy_url:
        # 风控红线：绝不静默换池出口。调用方必须显式传账号的注册代理。
        raise RuntimeError(
            "Ant 桥接要求显式代理：请传入账号的注册代理（proxy_used），"
            "节点直连会被 OpenAI 边缘封禁，随机换出口会触发风控")
    with _NODE_FLOW_LOCK:
        client = AntBrowserClient()
        pw = sync_playwright().start()
        opened = None
        page = None
        ctx = None
        browser = None
        try:
            opened = client.open_profile(proxy_url=proxy_url, email=email)
            cdp = opened.cdp_url or opened.direct_debug_url
            browser = pw.chromium.connect_over_cdp(cdp, timeout=30000)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            page.set_default_timeout(60000)
            yield page, ctx, opened
        finally:
            for closer in (
                lambda: page and page.close(),
                lambda: browser and browser.close(),
                pw.stop,
            ):
                try:
                    closer()
                except Exception:
                    pass
            try:
                client.cleanup_profile(opened)
            except Exception as exc:
                logger.warning("[Ant桥] 实例清理失败：%s", exc)


# ------------------------------------------------------------------
# 登录页状态探测（选择器参考 roxy_registration 的页面结构知识）
# ------------------------------------------------------------------

_LOGIN_BUTTON_SELECTORS = [
    "[data-testid='login-button']",
    "button:has-text('Log in')",
    "a:has-text('Log in')",
    "button:has-text('登录')",
    "a:has-text('登录')",
]
_EMAIL_INPUT_SELECTORS = [
    "input[name='email']",
    "input[type='email']",
    "input[autocomplete='username']",
]
_OTP_INPUT_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[name='code']",
    "input[inputmode='numeric']",
]
# 密码页（有密码账号的登录第二页；无密码账号走 OTP 页）
_PWD_INPUT_SELECTORS = [
    "input[type='password'][name='password']",
    "input[type='password'][autocomplete='current-password']",
    "input[type='password']",
]
# JS 点击 spec：css|||文本正则（querySelectorAll 不支持 :has-text()）
_EMAIL_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button[data-testid='submit-button']",
    "button|||^continue$|^继续$|sign in|log in|登录",
    "input[type='submit']",
]


def _visible_first(page, selectors, *, timeout_per_ms=3500):
    """按顺序探测选择器，返回第一个**可见**的匹配。

    注意不能用 locator.first 直接 is_visible：DOM 里第一个匹配可能藏在
    隐藏侧栏（如移动端菜单里的「登录」按钮），必须遍历匹配找可见项。
    """
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = min(loc.count(), 20)
            for i in range(count):
                item = loc.nth(i)
                try:
                    if item.is_visible():
                        return item
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _click_visible(page, selectors, *, timeout_per_ms=3500) -> bool:
    loc = _visible_first(page, selectors, timeout_per_ms=timeout_per_ms)
    if loc is None:
        return False
    loc.click()
    return True


def _type_slow(locator, text: str) -> None:
    locator.click()
    page_wait = 400
    time.sleep(page_wait / 1000)
    for ch in str(text):
        locator.type(ch, delay=int(60 + (hash(ch) % 60)))
    time.sleep(0.3)


_JS_FILL = """(args) => {
  const [css, val] = args;
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const els = [...document.querySelectorAll(css)].filter(visible);
  const el = els[els.length - 1];
  if (!el) return false;
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, val);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.focus();
  return true;
}"""

_JS_CLICK = """(cssList) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
  for (const spec of cssList) {
    const [css, pattern] = spec.split('|||');
    let els = [...document.querySelectorAll(css)].filter(visible).filter(enabled);
    if (pattern) {
      const re = new RegExp(pattern, 'i');
      els = els.filter(el => re.test((el.innerText || el.textContent || '').trim()));
    }
    if (els.length) { els[els.length - 1].click(); return spec; }
  }
  return null;
}"""


def _js_fill(page, css: str, value: str) -> bool:
    """JS 原生 setter 填值：绕开 React 表单重渲染导致的 detached 循环。

    OpenAI 登录页的输入框会被 React 不断重挂载，Playwright 点击等待
    "stable" 会一直重试直至 detached；协议同款的 JS setter + input 事件
    是稳定路径（roxy_registration 同款方案）。
    """
    try:
        return bool(page.evaluate(_JS_FILL, [css, value]))
    except Exception as exc:
        logger.debug("[Ant桥] js_fill %s 失败: %s", css, exc)
        return False


def _js_click_first(page, specs: list[str]) -> str | None:
    """specs: ['button[type=submit]', "button|||^继续$|^Continue$"]（css|||文本正则）。"""
    try:
        return page.evaluate(_JS_CLICK, list(specs))
    except Exception as exc:
        logger.debug("[Ant桥] js_click 失败: %s", exc)
        return None


_JS_FOCUS = """(css) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const els = [...document.querySelectorAll(css)].filter(visible);
  const el = els[els.length - 1];
  if (!el) return false;
  el.focus();
  el.select && el.select();
  return true;
}"""


def _human_type(page, css: str, text: str, *, verify: bool = True) -> bool:
    """真实键盘输入：JS focus 后 keyboard.type 逐字符。

    2026-11-20 灰度实证：JS setter 一次性设值 + JS click 提交验证码时，
    OpenAI /email-verification POST 直接返回 HTTP 500（页面显示
    「auth.openai.com 目前无法处理此请求」）——服务端行为遥测判定非真人
    输入。注册流程（selenium send_keys 真实键事件）同表单可过，因此这里
    改用 CDP 键盘事件（keydown/keypress/input 全套）+ 随机节奏。
    """
    import random as _random

    for attempt in (1, 2):
        try:
            if not page.evaluate(_JS_FOCUS, css):
                return False
            time.sleep(_random.uniform(0.25, 0.5))
            page.keyboard.type(str(text), delay=_random.randint(85, 160))
            time.sleep(_random.uniform(0.3, 0.6))
            if not verify:
                return True
            got = page.evaluate("""(css) => {
              const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const els = [...document.querySelectorAll(css)].filter(visible);
              return els.length ? (els[els.length - 1].value || '') : null;
            }""", css)
            # 元素消失(got=None)按失败重试：之前把 None 当成功放行过——
            # 会话过期页根本没有邮箱框，"输入成功"是假的，提交自然无效。
            if got is not None and str(got).strip() == str(text).strip():
                return True
            logger.info("[Ant桥] human_type 第 %s 次输入未落实(got=%r)，重试", attempt, got)
        except Exception as exc:
            logger.debug("[Ant桥] human_type 异常(第 %s 次)：%s", attempt, exc)
    return False


def _human_submit(page, specs: list[str], *, prefer_enter: bool = True) -> bool:
    """提交表单：优先回车键，其次 force 点击，最后 JS click。

    2026-11-20 实证教训：坐标点击在 React 重渲染间隙会静默点空（取坐标
    和按下鼠标之间按钮移位，mouse.click 不报错但表单从未提交），页面上
    表现为"码填了却不动"，极难排查。回车键不依赖坐标（输入框敲完自带
    焦点）且是纯真实键盘事件；force=True 的 locator.click 会在点击瞬间
    原子重定位元素，也是可靠兜底。
    """
    if prefer_enter:
        try:
            page.keyboard.press("Enter")
            time.sleep(0.4)
            return True
        except Exception as exc:
            logger.debug("[Ant桥] human_submit 回车失败：%s", exc)

    # force 点击：跳过 actionability 等待，点下瞬间重定位，避免重渲染竞态
    for spec in specs:
        css = spec.split("|||")[0]
        pattern = spec.split("|||")[1] if "|||" in spec else ""
        try:
            loc = page.locator(css)
            count = min(loc.count(), 20)
            for i in range(count):
                item = loc.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    if pattern:
                        import re as _re

                        text = (item.inner_text(timeout=1500) or "").strip()
                        if not _re.search(pattern, text, _re.I):
                            continue
                    item.click(force=True, timeout=4000)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return _js_click_first(page, specs) is not None


def _force_click_by_text(page, text_patterns: list[str]) -> str | None:
    """按文本正则真点击（locator force，真实鼠标事件）。返回命中的 selector。"""
    import re as _re

    sels = ["a", "button", "[role=button]", "[role=link]"]
    for pat in text_patterns:
        rx = _re.compile(pat, _re.I)
        for tag in sels:
            try:
                loc = page.locator(tag)
                for i in range(min(loc.count(), 30)):
                    item = loc.nth(i)
                    try:
                        if not item.is_visible():
                            continue
                        text = (item.inner_text(timeout=800) or "").strip()
                        if text and rx.search(text):
                            item.click(force=True, timeout=3000)
                            return f"{tag}:{text[:20]}"
                    except Exception:
                        continue
            except Exception:
                continue
    return None


def _click_entry_button(page) -> bool:
    """会话过期页/落地页的「登录」「继续登录」入口按钮。

    locator force 点击（点下瞬间原子重定位，抗 React 重渲染）优先，
    JS click 兜底。返回是否点到了。
    """
    import re as _re

    for sel in ("button:has-text('登录')", "a:has-text('登录')",
                "button:has-text('继续登录')", "button:has-text('Log in')",
                "a:has-text('Log in')"):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 8)):
                item = loc.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    text = (item.inner_text(timeout=1000) or "").strip()
                    if not _re.match(r"^(登录|继续登录|log in|continue)$", text, _re.I):
                        continue
                    item.click(force=True, timeout=3000)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return _js_click_first(page, ["button|||^登录$|^log in$|^继续登录$|continue",
                                  "a|||^登录$|^log in$|^继续登录$"]) is not None


_TOTP_WINDOW = 30               # 动态码的时间窗（秒）
_TOTP_MIN_SECONDS_LEFT = 12     # 窗口剩余不足这么多秒，就等到下一个窗口再取码
_MFA_MAX_ATTEMPTS = 3


def _fresh_totp(email: str, *, previous: str = "") -> str:
    """取一个新鲜的动态码；库里没有密钥时返回空串。

    敲码是拟人节奏（每位 85~160ms），窗口末尾取的码往往还没提交就过期；
    上一个码刚被拒，再敲同一个码也没有意义。这两种情况都等到下一个窗口再取。
    """
    from core.codex_oauth import _account_totp_code

    while True:
        code = _account_totp_code(email) or ""
        seconds_left = _TOTP_WINDOW - (time.time() % _TOTP_WINDOW)
        if not code or (seconds_left >= _TOTP_MIN_SECONDS_LEFT and code != previous):
            return code
        time.sleep(seconds_left + 0.5)


def _handle_mfa_if_present(page, email: str, *, timeout: float = 25.0) -> bool:
    """mfa-challenge 页（TOTP）：现算动态码真实键盘敲入 + 回车。

    有 2FA 的账号在密码/OTP 后会出现；无 2FA 则该函数静默跳过。
    动态码没被接受会换新码重试，见 _submit_totp。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = str(page.url or "")
        if "mfa-challenge" not in url:
            # 也可能是输入框已渲染但 URL 未变，双条件探测
            if not _visible_first(page, ["input[name='code'][autocomplete='one-time-code']"], timeout_per_ms=1500):
                return False
        css = None
        for c in ("input[name='code']", "input[autocomplete='one-time-code']"):
            try:
                if page.evaluate("(css)=>{const v=el=>!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);return [...document.querySelectorAll(css)].some(v)}", c):
                    css = c
                    break
            except Exception:
                pass
        if not css:
            time.sleep(1.5)
            continue
        _submit_totp(page, email, css)
        return True
    return False


def _submit_totp(page, email: str, css: str) -> None:
    """敲入动态码并提交；页面还停在质询页就换一个新码重来。离开质询页后返回，否则抛错。

    2026-11-24 实测：同一个账号、同一步骤，一次敲完没离开质询页，一次十几秒就过了，
    原因从日志里看不出来（当时没留下页面现场）。原实现只敲一次然后死等 60 秒。
    现在每轮等 20 秒：输入框还在就说明这个码没被接受，换个新码再来，最多 3 次。
    """
    used = ""
    for attempt in range(1, _MFA_MAX_ATTEMPTS + 1):
        code = _fresh_totp(email, previous=used)
        if not code:
            raise RuntimeError(f"{email} 出现 mfa-challenge 但没有 totp_secret")
        used = code
        logger.info("[Ant桥] %s MFA 质询，敲入 TOTP（第 %s/%s 次）", email, attempt, _MFA_MAX_ATTEMPTS)
        if not _human_type(page, css, code):
            raise RuntimeError("TOTP 敲入失败")
        time.sleep(0.8)
        _human_submit(page, _EMAIL_SUBMIT_SELECTORS)
        for _ in range(10):
            time.sleep(2)
            if "mfa-challenge" not in str(page.url or ""):
                return
        if attempt == _MFA_MAX_ATTEMPTS:
            break
        if _otp_input_css(page) is None:
            # 输入框没了却还停在质询页：不是「码不对」，再敲也没有意义
            time.sleep(10)
            if "mfa-challenge" not in str(page.url or ""):
                return
            break
        logger.info("[Ant桥] %s 动态码未被接受，换新码重试", email)
    raise RuntimeError(f"MFA 提交后未离开质询页（已试 {_MFA_MAX_ATTEMPTS} 次）: {page.url[:80]}")


def _select_workspace_if_present(page, *, timeout: float = 20.0) -> bool:
    """workspace 选择页（团队账号登录尾步）：选付费团队空间。

    生产惯例：优先 paid_workspace_account_id 对应的 MyTeam 卡片；
    找不到再选个人账户。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "/workspace" not in str(page.url or ""):
            return False
        clicked = None
        for text in ("MyTeam", "个人账户", "Personal"):
            try:
                for sel in (f"button:has-text('{text}')", f"[role=button]:has-text('{text}')"):
                    loc = page.locator(sel)
                    for i in range(min(loc.count(), 10)):
                        item = loc.nth(i)
                        try:
                            if item.is_visible():
                                item.click(force=True, timeout=4000)
                                clicked = text
                                break
                        except Exception:
                            continue
                    if clicked:
                        break
                if clicked:
                    break
            except Exception:
                continue
        if clicked:
            logger.info("[Ant桥] 已选工作空间：%s", clicked)
            time.sleep(6)
            _wait_out_challenge(page, timeout=45.0, step="after_ws")
            return True
        time.sleep(1.5)
    return False


def _is_logged_in(page) -> bool:
    url = (page.url or "").lower()
    if "auth.openai.com" in url:
        return False
    if _visible_first(page, ["[data-testid='settings-button']", "[data-testid='profile-button']"], timeout_per_ms=2500):
        return True
    return False


# CF 质询检测（移植 roxy_registration.detect_challenge 的 JS，Playwright 版）
_CHALLENGE_JS = """() => {
  const title = (document.title || '').toLowerCase();
  const url = location.href || '';
  const interstitialTitle = ['just a moment', 'checking your browser', '请稍候', '正在验证', '少々お待ち', '確認しています']
    .some(t => title.includes(t));
  const interstitialNode = !!document.querySelector(
    '#cf-challenge-running, #challenge-running, #challenge-form, #cf-please-wait, .cf-browser-verification');
  const widget = [...document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]')]
    .some(f => !!(f.offsetWidth || f.offsetHeight || f.getClientRects().length));
  const widgetNode = !!document.querySelector('.cf-turnstile, #cf-chl-widget, [id^="cf-chl-widget"]');
  const urlMark = url.includes('__cf_chl') || url.includes('/cdn-cgi/challenge-platform');
  let kind = '';
  if (interstitialTitle || interstitialNode || urlMark) kind = 'interstitial';
  else if (widget || widgetNode) kind = 'widget';
  return {active: !!kind, kind, title: document.title || '', url};
}"""


def _detect_challenge(page) -> dict:
    try:
        res = page.evaluate(_CHALLENGE_JS) or {}
        return res if isinstance(res, dict) else {}
    except Exception:
        # 页面正在跳转/崩溃时读取失败按无质询处理，交给上层重载逻辑
        return {}


def _wait_out_challenge(page, *, timeout: float = 60.0, done=None, step: str = "") -> bool:
    """检测到 CF 人机验证就安静等它跑完；刷新会打断 Turnstile 从头再来，绝不刷新。

    返回 True 表示确实等过一次挑战（无论是否通过）。
    """
    first = _detect_challenge(page)
    if not first.get("active"):
        return False
    logger.info("[Ant桥] 检测到 CF 人机验证(%s)%s，安静等待最长 %.0fs：%s",
                first.get("kind") or "?", f" @ {step}" if step else "",
                timeout, str(first.get("title") or "")[:50])
    started = time.time()
    while time.time() - started < timeout:
        time.sleep(2.5)
        if done is not None:
            try:
                if done():
                    logger.info("[Ant桥] 质询期间页面已就绪，用时 %.1fs", time.time() - started)
                    return True
            except Exception:
                pass
        if not _detect_challenge(page).get("active"):
            logger.info("[Ant桥] CF 质询已通过，用时 %.1fs", time.time() - started)
            return True
    logger.warning("[Ant桥] CF 质询等待超时(%.0fs)%s", timeout, f" @ {step}" if step else "")
    return True


def _heal_chrome_error(page, *, max_reloads: int = 2, back_url: str = "https://auth.openai.com/log-in") -> bool:
    """chrome-error:// 页面自愈：导航失败后不能 reload（重载的是错误页本身），
    必须 goto 回真实入口。重载后可能再撞 CF 质询，由调用方的质询等待接手。
    """
    reloaded = 0
    while reloaded < max_reloads:
        url = str(page.url or "")
        if "chrome-error" not in url:
            return reloaded > 0
        reloaded += 1
        logger.info("[Ant桥] 页面进入 chrome-error，%.1fs 后回跳 %s(%s/%s)",
                    3.0, back_url[:60], reloaded, max_reloads)
        time.sleep(3.0)
        try:
            page.goto(back_url, timeout=45000, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("[Ant桥] 回跳失败：%s", str(exc)[:120])
        time.sleep(2.0)
    return reloaded > 0


def ant_login_cookies(email: str, proxy: str | None, otp_waiter, *,
                      email_source: str | None = None) -> tuple[list[dict], dict]:
    """节点浏览器完成邮箱登录，返回 (cookies, info)。

    登录路径（2026-11-20 实测）：直接打开 auth.openai.com/log-in。
    chatgpt.com 首页的内联登录面板提交后不动（面板消失无导航），不可用；
    auth.openai.com 的标准页行为可靠：
      会话过期落地页(「你的会话已结束」) → 点「登录」
      → 「继续登录」邮箱页 → 提交
      → 「检查你的收件箱」验证码页（input[name=code]）→ 提交
      → 跳回 chatgpt.com 登录态。
    otp_waiter(after_ts) -> str：与协议流程同一等待器（生产侧收邮件 OTP）。
    显式传入 proxy 时不重试换代理；未指定时代理池候选逐个试（最多 3 轮）。
    """
    if not callable(otp_waiter):
        raise ValueError("otp_waiter 必须可调用：otp_waiter(after_ts) -> code")
    email = str(email or "").strip()
    explicit_proxy = bool(str(proxy or "").strip())
    last_error: Exception | None = None

    for attempt in range(1, 4 if not explicit_proxy else 2):
        try:
            return _ant_login_once(email, proxy, otp_waiter, email_source, attempt)
        except Exception as exc:
            last_error = exc
            logger.warning("[Ant桥] 登录尝试 %s 失败（proxy=%s）：%s",
                           attempt, (proxy or "pool")[:40], str(exc)[:160])
            # 未取到登录验证码（OTP 邮件空）不换代理重试——会重复发信；
            # 页面导航/结构类失败大概率是代理质量，换代理再试。
            if explicit_proxy or "未取到登录验证码" in str(exc):
                raise
            time.sleep(2)
    raise last_error or RuntimeError("登录失败")


def _otp_input_css(page) -> str | None:
    """页面上第一个可见的验证码输入框的 css；没有返回 None。"""
    for css in _OTP_INPUT_SELECTORS:
        try:
            if page.evaluate("""(css) => {
              const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              return [...document.querySelectorAll(css)].some(visible);
            }""", css):
                return css
        except Exception:
            pass
    return None


def _feed_login_otp(page, email: str, otp_waiter) -> bool:
    """登录尾段的邮箱验证页：有验证码框就取码敲入并提交，返回是否提交了一轮。

    异常直接向上抛：这一步失败没有重试的意义，调用方应立刻结束而不是等到超时。
    """
    css = _otp_input_css(page)
    if not css:
        return False
    code = str(otp_waiter(time.time()) or "").strip()
    if not code:
        raise RuntimeError(f"{email} 登录尾段的邮箱验证码未取到")
    if not _human_type(page, css, code):
        raise RuntimeError(f"{email} 登录尾段的邮箱验证码填写失败")
    time.sleep(0.6)
    if not _human_submit(page, _EMAIL_SUBMIT_SELECTORS):
        page.keyboard.press("Enter")
    logger.info("[Ant桥] %s 登录尾段提交了邮箱验证码", email)
    return True


def _ant_login_once(email: str, proxy: str | None, otp_waiter,
                    email_source: str | None, attempt: int) -> tuple[list[dict], dict]:
    with _ant_page(proxy, email=email) as (page, ctx, opened):
        page.goto("https://auth.openai.com/log-in", timeout=60000, wait_until="domcontentloaded")
        time.sleep(4)
        deadline = time.time() + _LOGIN_TIMEOUT

        # 首跳常撞 CF 质询（Just a moment / 少々お待ちください）：安静等它过，
        # chrome-error（代理抖动）则重载自愈后再等质询。
        _wait_out_challenge(page, timeout=75.0, step="login_page")
        _heal_chrome_error(page)
        _wait_out_challenge(page, timeout=60.0, step="login_page_after_reload")

        # 会话过期落地页可能有「登录」按钮，先进到邮箱页。
        # 顺序：先确认邮箱框可见才输入；不可见则点「登录」入口。
        # 输入一律真实键盘事件（_human_type 的值验证已收紧：元素消失=失败）。
        email_css = None
        while time.time() < deadline:
            if _detect_challenge(page).get("active"):
                _wait_out_challenge(page, timeout=60.0, step="email_wait")
                continue
            if "chrome-error" in str(page.url or ""):
                _heal_chrome_error(page)
                continue
            visible_css = None
            for css in _EMAIL_INPUT_SELECTORS:
                try:
                    if page.evaluate("(css)=>{const v=el=>!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);return [...document.querySelectorAll(css)].some(v)}", css):
                        visible_css = css
                        break
                except Exception:
                    continue
            if visible_css:
                if _human_type(page, visible_css, email):
                    email_css = visible_css
                    break
                time.sleep(1.0)
                continue
            # 入口按钮（会话过期页「登录」/「继续登录」）：locator force 原子
            # 点击优先（JS click 是间歇性的——React 重渲染窗口会点空）
            if _click_entry_button(page):
                time.sleep(5)
                continue
            time.sleep(1.5)
        if not email_css:
            raise RuntimeError(f"登录页邮箱输入未就绪: url={page.url}")
        time.sleep(0.6)
        after_ts = time.time()
        if not _human_submit(page, _EMAIL_SUBMIT_SELECTORS):
            page.keyboard.press("Enter")

        # 等第二页：有密码账号可走「使用密码继续」链接（注册实测
        # パスワードで続行/使用密码继续 → 密码页，无需 OTP 邮件）；
        # 无密码账号出现验证码页（「检查你的收件箱」）。
        #
        # 「使用密码继续」只有库里有密码时才能走。库里的密码可能是空的（早期注册没记
        # 密码），而账号在 OpenAI 侧其实有密码——验证码页上照样有这个入口，点进去只会
        # 落到密码页然后卡死（2026-11-24 账号 #186 绑 2FA 时实测）。验证码登录对这类
        # 账号同样可用，所以没密码就别点，直接走验证码。
        from core.db import _extract_registration_password

        try:
            from core import db as _db

            acc = _db.get_account_by_email(email) or {}
        except Exception:
            acc = {}
        password = _extract_registration_password(acc) if acc else ""
        prefer_password = bool(password)
        otp_css = None
        pwd_css = None
        while time.time() < deadline:
            if _detect_challenge(page).get("active"):
                _wait_out_challenge(page, timeout=60.0, step="otp_page_wait")
                continue
            if "chrome-error" in str(page.url or ""):
                _heal_chrome_error(page)
                continue
            for css in _PWD_INPUT_SELECTORS:
                probe = page.evaluate("""(css) => {
                  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                  const els = [...document.querySelectorAll(css)].filter(visible);
                  return els.length ? true : false;
                }""", css)
                if probe:
                    pwd_css = css
                    break
            if pwd_css:
                break
            # 「使用密码继续」链接：有密码账号的最优路径（避开 OTP 邮件依赖）。
            # 必须 locator force 真点击——JS click 触发的跳转实测被服务端 500
            # （/log-in/password GET 返回 HTTP ERROR 500，两轮复现）。
            pw_link = _force_click_by_text(page, [
                r"password|密码で続行|使用密码继续|continue with password",
            ]) if prefer_password else None
            if pw_link:
                logger.info("[Ant桥] %s 真点击了「使用密码继续」(%s)", email, pw_link)
                time.sleep(4)
                continue
            for css in _OTP_INPUT_SELECTORS:
                probe = page.evaluate("""(css) => {
                  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                  const els = [...document.querySelectorAll(css)].filter(visible);
                  return els.length ? true : false;
                }""", css)
                if probe:
                    otp_css = css
                    break
            if otp_css:
                # OTP 框先出现时别急着定分支：「使用密码继续」链接渲染更慢，
                # 而有密码账号的 OTP 通道可能不可用（outlook 池 IMAP 已坏）。
                # 等最多 10s 找密码链接，找到优先走密码路径。
                if prefer_password:
                    for _ in range(10):
                        pw_link2 = _force_click_by_text(page, [
                            r"password|密码で続行|使用密码继续|continue with password",
                        ])
                        if pw_link2:
                            logger.info("[Ant桥] %s OTP 页上改走「使用密码继续」", email)
                            otp_css = None
                            time.sleep(4)
                            break
                        time.sleep(1.0)
                if otp_css:
                    break
            time.sleep(1.0)

        if pwd_css:
            # 密码路径：用上面从库里取到的账号密码，真实键盘敲入 + 回车
            if not password:
                raise RuntimeError(f"{email} 出现密码页但库里没有密码")
            logger.info("[Ant桥] %s 走密码登录页", email)
            if not _human_type(page, pwd_css, password):
                raise RuntimeError("密码敲入失败")
            time.sleep(0.6)
            if not _human_submit(page, _EMAIL_SUBMIT_SELECTORS):
                page.keyboard.press("Enter")
            # 密码对了可能还有 TOTP（有 2FA 的账号）
            _handle_mfa_if_present(page, email)
            _select_workspace_if_present(page)
        elif not otp_css:
            raise RuntimeError(f"邮箱提交后未进入验证码页: url={page.url}")

        # ↓ 仅 OTP 路径走验证码段；密码路径已完成登录，直接到 MFA/会话等待
        # 这两个变量尾段也要用。密码路径以前没定义它们：登录成功时按 cookie 判定会
        # NameError，被兜底吞掉，全靠后面的 session 探针顶着。
        last_submit_ts = 0.0
        otp_round = 0
        if otp_css:
            code = str(otp_waiter(after_ts) or "").strip()
            if not code:
                raise RuntimeError("未取到登录验证码")
            if not _human_type(page, otp_css, code):
                raise RuntimeError("验证码填写失败")
            time.sleep(0.6)
            if not _human_submit(page, _EMAIL_SUBMIT_SELECTORS):
                page.keyboard.press("Enter")

            # 验证码可能要填多轮：新设备/IP 风控下 OpenAI 会在提交第一轮后跳到
            # /email-verification 再发一封。每轮：等页面离开 auth.openai.com（登录
            # 完成）或出现新一轮 OTP 框（提交后至少隔 8s 才算新一轮）。
            # MFA 动态码页和工作空间选择页不是邮箱验证：MFA 页上的动态码框也是数字框，
            # 不看地址就会把它当成第 2 轮验证码，去等一封永远不会来的邮件直到超时
            # （2026-11-24 账号 #186 补密码时实测）。这两类页面交给后面的 MFA / 工作空间处理。
            last_submit_ts = time.time()
            last_url = str(page.url or "")
            otp_round = 1
            while time.time() < deadline and otp_round < 4:
                new_round = False
                while time.time() < deadline:
                    url = str(page.url or "")
                    if url != last_url:
                        last_url = url
                        logger.info("[Ant桥] %s OTP 提交后页面跳转：%s", email, url[:100])
                    if (("chatgpt.com" in url and "/auth/" not in url)
                            or "mfa" in url.lower() or "/workspace" in url.lower()):
                        new_round = False
                        break
                    otp_still = False
                    for css in _OTP_INPUT_SELECTORS:
                        try:
                            if page.evaluate("""(css) => {
                              const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                              return [...document.querySelectorAll(css)].some(visible);
                            }""", css):
                                otp_still = True
                                break
                        except Exception:
                            pass
                    if otp_still and time.time() - last_submit_ts > 8:
                        new_round = True
                        break
                    time.sleep(1.5)
                if not new_round:
                    break
                otp_round += 1
                logger.info("[Ant桥] %s 出现第 %s 轮邮箱验证，等新验证码", email, otp_round)
                code = str(otp_waiter(time.time()) or "").strip()
                if not code:
                    raise RuntimeError(f"第 {otp_round} 轮验证码未取到")
                if not _human_type(page, otp_css, code):
                    raise RuntimeError(f"第 {otp_round} 轮验证码填写失败")
                time.sleep(0.6)
                if not _human_submit(page, _EMAIL_SUBMIT_SELECTORS):
                    page.keyboard.press("Enter")
                last_submit_ts = time.time()

        # OTP 路径提交后同样可能撞 MFA / 工作空间选择（有 2FA 或团队账号）
        _handle_mfa_if_present(page, email)
        _select_workspace_if_present(page)

        # 等登录态：以 cookie 里的 session-token 为准（与 vcc_pay_session 同判据）。
        # OTP 提交后的多跳重定向（authorize→callback→chatgpt.com）是质询高发区：
        # 撞质询安静等；chrome-error 重载自愈；cookie 在 context 里，页面死活不影响导出。
        last_note = ""
        while time.time() < deadline:
            time.sleep(2.0)
            try:
                cookies = _export_cookies(ctx)
                if any("session-token" in str(c.get("name") or "") for c in cookies):
                    logger.info("[Ant桥] %s 登录成功(attempt=%s, otp_rounds=%s)，导出 %s 个 cookies",
                                email, attempt, otp_round, len(cookies))
                    return cookies, {"login": "browser_pwd" if pwd_css else "browser_otp",
                                     "profile_id": opened.profile_id, "proxy": proxy}
            except Exception:
                pass
            try:
                # 兜底：页面已回 chatgpt.com 时直接问 /api/auth/session
                url0 = str(page.url or "")
                if "chatgpt.com" in url0 and "/auth/" not in url0 and "chrome-error" not in url0:
                    ident = page.evaluate("""async () => {
                        try {
                          const r = await fetch('/api/auth/session', {credentials: 'include'});
                          if (!r.ok) return null;
                          const j = await r.json();
                          return (j.user && j.user.email) || null;
                        } catch (e) { return null; }
                    }""")
                    if ident:
                        cookies = _export_cookies(ctx)
                        logger.info("[Ant桥] %s 登录成功(session探针=%s)，导出 %s 个 cookies",
                                    email, str(ident)[:40], len(cookies))
                        return cookies, {"login": "browser_pwd", "profile_id": opened.profile_id,
                                         "proxy": proxy}
            except Exception:
                pass
            # 密码登录后被要求再验一次邮箱（换了出口/设备时常见）：页面停在
            # /email-verification 要验证码。密码路径原本不处理这一步，只会干等到超时
            # （2026-11-24 账号 #185 绑 2FA 时实测）。放在兜底 try 之外：
            # 取不到码要立刻失败，不能被吞成继续等。
            if "email-verification" in str(page.url or "") and time.time() - last_submit_ts > 8:
                if otp_round >= 4:
                    raise RuntimeError(f"{email} 邮箱验证码已提交 {otp_round} 轮仍未通过")
                if _feed_login_otp(page, email, otp_waiter):
                    otp_round += 1
                    last_submit_ts = time.time()
                    continue
            try:
                url = str(page.url or "")
                note = f"{url[:70]}"
                if note != last_note:
                    last_note = note
                    logger.info("[Ant桥] %s 登录尾段页面：%s", email, note)
                if "mfa-challenge" in url:
                    # 慢渲染的 MFA 页在尾段接住（密码/OTP 提交后可能几秒才出现）
                    _handle_mfa_if_present(page, email, timeout=45.0)
                    continue
                if "/workspace" in url:
                    _select_workspace_if_present(page)
                    continue
                if _detect_challenge(page).get("active"):
                    _wait_out_challenge(page, timeout=75.0, step="post_otp_redirect",
                                        done=lambda: any("session-token" in str(c.get("name") or "")
                                                         for c in _export_cookies(ctx)))
                    continue
                if "chrome-error" in url:
                    _heal_chrome_error(page)
                    _wait_out_challenge(page, timeout=60.0, step="post_reload")
                    continue
            except Exception:
                pass
        raise RuntimeError(f"登录后未回到 chatgpt.com 登录态: url={page.url}")


def _export_cookies(context) -> list[dict]:
    try:
        return context.cookies(["https://chatgpt.com/", "https://auth.openai.com/"])
    except Exception:
        return context.cookies()


def session_with_cookies(email: str, proxy: str | None, cookies: list[dict]):
    """用节点浏览器 cookies 建 BrowserSession（同代理保证 CF cookie/IP 连续）。"""
    from core.session import BrowserSession

    identity = str(email or "").strip().lower()
    session = BrowserSession(proxy=str(proxy or "").strip() or None,
                             fingerprint_seed=f"account:{identity}")
    jar = session.session.cookies
    for c in cookies or []:
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        domain = str(c.get("domain") or "").lstrip(".")
        path = str(c.get("path") or "/") or "/"
        if not name or not domain:
            continue
        try:
            jar.set(name, value, domain=domain, path=path)
        except Exception as exc:
            logger.debug("[Ant桥] cookie 注入跳过 %s: %s", name, exc)
    logger.info("[Ant桥] %s BrowserSession 已注入节点 cookies", email)
    return session


# ------------------------------------------------------------------
# 补密码：混合流程
# ------------------------------------------------------------------

def run_password_backfill_hybrid(email: str, *, proxy: str | None,
                                 new_password: str, otp_waiter,
                                 email_source: str | None = None):
    """节点浏览器登录 → 协议 reauth/OTP/提交。

    返回 (BrowserSession, {"password": ..., "add": ...})，与
    core.password_reset.run_password_backfill 同构。

    代理策略（风控红线）：一号一代理。必须用账号的注册代理（proxy_used）
    —— 浏览器登录与协议收尾同 IP；**绝不跨代理轮换重试**，否则账号在
    不同国家/地区反复登录必触发风控。代理失败就让本流程失败并暴露出来，
    由人工确认代理可用后再试。
    """
    from core.password_reset import (
        trigger_add_password_reauth, validate_email_otp, submit_new_password,
    )

    proxy = str(proxy or "").strip() or None
    if not proxy:
        # 入口没带代理时按邮箱回查注册代理，仍无才报错（不静默换池出口）
        proxy = registration_proxy_for(email)
    if not proxy:
        raise RuntimeError(
            f"{email} 没有可用的注册代理（proxy_used 为空）：一号一代理是风控红线，"
            "请先为该账号确认固定出口后再补密码")

    logger.info("[Ant桥] 固定注册代理执行补密码：%s", proxy[:50])
    cookies, login_info = ant_login_cookies(email, proxy, otp_waiter,
                                            email_source=email_source)
    used_proxy = str(login_info.get("proxy") or proxy)
    session = session_with_cookies(email, used_proxy, cookies)
    logger.info("[Ant桥] 浏览器登录完成(%s)，协议侧发起补密码 reauth",
                login_info.get("login"))
    after_ts = trigger_add_password_reauth(session, email)
    code = str(otp_waiter(after_ts) or "").strip()
    if not code:
        raise RuntimeError("未取到 reauth 验证码")
    validate_email_otp(session, code)
    add_result = submit_new_password(session, email, new_password)
    return session, {"password": new_password, "add": add_result}


# ------------------------------------------------------------------
# 换卡：节点 page（替代 CloakBrowser）
# ------------------------------------------------------------------

@contextmanager
def ant_rebind_page(proxy_url: str | None, cookies: list[dict], email: str | None = None):
    """换卡浏览器页：节点实例 + cookie 注入，yield Playwright page。

    与 card_rebind_service 的 CloakBrowser 分支同构：
      page.set_default_timeout / add_cookies / new_page 均已就绪。
    email 用于实例命名（约定=账号邮箱）。
    """
    with _ant_page(proxy_url, email=email) as (page, ctx, opened):
        if cookies:
            try:
                ctx.add_cookies(list(cookies))
            except Exception as exc:
                logger.warning("[Ant桥] 换卡 cookie 注入失败（继续 SSO 登录链）：%s", exc)
        page.set_default_timeout(int(getattr(_ant_cfg, "ANT_CARD_PAGE_TIMEOUT", 60)) * 1000)
        yield page
