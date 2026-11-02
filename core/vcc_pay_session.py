# -*- coding: utf-8 -*-
"""会话式 VCC 支付：填好表单后暂停，等人确认再提交。

与 vcc_checkout_pay.pay_checkout 的一次性流程不同，这里浏览器要跨多个 HTTP
请求存活，所以每个会话独占一个线程，外部通过队列投递命令。

为什么必须独占线程：Playwright 同步 API 把对象绑定在创建它的线程（greenlet）
上，换个线程碰同一个 page 会直接抛 "Cannot switch to a different thread"。
Flask 每个请求可能落在不同工作线程，所以不能让请求线程直接操作 page。

资金闸门沿用 vcc_checkout_pay.check_amount_gate：金额不符不会进入可确认状态，
确认接口也会二次校验，确保"看到的"和"提交的"是同一个数。
"""
from __future__ import annotations

import base64
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 会话状态
ST_PREPARING = "preparing"            # 正在开浏览器、取卡、填表
ST_AWAITING_CONFIRM = "awaiting_confirm"  # 填好了，等人点确认
ST_SUBMITTING = "submitting"          # 已确认，正在提交
ST_AWAITING_OTP = "awaiting_otp"      # 需要人工输入 3DS 验证码
ST_AWAITING_HUMAN = "awaiting_human"  # 需要人直接操作页面（人机验证等）
ST_DONE = "done"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"

_TERMINAL = {ST_DONE, ST_FAILED, ST_CANCELLED}

# 固定视口：前端按这个尺寸换算点击坐标，不固定的话坐标会对不上。
VIEWPORT_W = 1280
VIEWPORT_H = 900


@dataclass
class PaySession:
    sid: str
    account_id: int
    email: str = ""
    state: str = ST_PREPARING
    message: str = ""
    currency: str = ""
    amount: float | None = None
    expected: float | None = None
    card_mask: str = ""
    gate_error: str = ""          # 闸门未通过的原因；非空时禁止确认
    shot_b64: str = ""            # 最新截图，base64，不落盘
    submitted: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    _cmd: queue.Queue = field(default_factory=queue.Queue, repr=False)
    _thread: Any = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        """给前端的视图。不含卡号明文，只有掩码。"""
        return {
            "session_id": self.sid,
            "account_id": self.account_id,
            "email": self.email,
            "state": self.state,
            "message": self.message,
            "currency": self.currency,
            "amount": self.amount,
            "expected": self.expected,
            "card_mask": self.card_mask,
            "gate_error": self.gate_error,
            "can_confirm": self.state == ST_AWAITING_CONFIRM and not self.gate_error,
            "needs_human": self.state == ST_AWAITING_HUMAN,
            "viewport": {"w": VIEWPORT_W, "h": VIEWPORT_H},
            "submitted": self.submitted,
            "screenshot": self.shot_b64,
            "age": int(time.time() - self.created_at),
        }


_sessions: dict[str, PaySession] = {}
_lock = threading.Lock()


def _touch(sess: PaySession, **kw) -> None:
    for key, value in kw.items():
        setattr(sess, key, value)
    sess.updated_at = time.time()


def _shoot(page: Any, sess: PaySession, *, full: bool = True) -> None:
    """截图直接转 base64 存内存。截图含卡后四位和账单地址，不写磁盘。"""
    try:
        raw = page.screenshot(full_page=full)
        sess.shot_b64 = base64.b64encode(raw).decode()
        sess.updated_at = time.time()
    except Exception as exc:  # noqa: BLE001 - 截图失败不该打断支付
        logger.debug("[pay-session] 截图失败：%s", exc)


def _try_cloak_browser(cfg, proxy: str, sess):
    """尝试用 CloakBrowser 启动，失败返回 None 由调用方退回裸 Playwright。

    CloakBrowser 带指纹伪装、humanize 鼠标轨迹和 geoip 时区匹配，项目注册
    流程靠它过 Cloudflare。裸 Chromium 会被 Stripe 风控和发卡行的 3DS 判成
    自动化环境，表现为 "unable to authenticate your payment method"。

    注意：它自带 Playwright 实例，必须在 sync_playwright() 之外调用。
    """
    if not bool(getattr(cfg, "VCC_USE_CLOAK", True)):
        return None
    try:
        from cloakbrowser import launch as cloak_launch
    except ImportError:
        logger.warning("[pay-session] %s 未安装 cloakbrowser，退回裸 Playwright", sess.sid)
        return None

    opts: dict[str, Any] = {
        "headless": not bool(getattr(cfg, "VCC_SERVER_HEADED", False)),
        "humanize": bool(getattr(cfg, "VCC_CLOAK_HUMANIZE", True)),
        "geoip": bool(getattr(cfg, "VCC_CLOAK_GEOIP", True)),
    }
    if proxy:
        opts["proxy"] = proxy
    seed = (getattr(cfg, "VCC_CLOAK_SEED", "") or "").strip()
    if seed:
        opts["args"] = [f"--fingerprint={seed}"]
    try:
        from config import cloak as cloak_cfg
        key = str(getattr(cloak_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
        if key:
            opts["license_key"] = key
    except Exception:  # noqa: BLE001 - 没有配置就按未授权跑
        pass

    try:
        browser = cloak_launch(**opts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[pay-session] %s CloakBrowser 启动失败，退回裸 Playwright：%s",
                       sess.sid, str(exc)[:160])
        return None
    logger.info("[pay-session] %s 使用 CloakBrowser（humanize=%s geoip=%s headless=%s）",
                sess.sid, opts["humanize"], opts["geoip"], opts["headless"])
    return browser


def _plain_browser(pw, cfg, proxy: str):
    """裸 Playwright 回退路径。"""
    from core.vcc_checkout_pay import build_proxy_option

    launch: dict[str, Any] = {
        "headless": not bool(getattr(cfg, "VCC_SERVER_HEADED", False)),
    }
    channel = (getattr(cfg, "VCC_BROWSER_CHANNEL", "") or "").strip()
    exec_path = (getattr(cfg, "VCC_BROWSER_PATH", "") or "").strip()
    if channel:
        launch["channel"] = channel
    elif exec_path:
        launch["executable_path"] = exec_path
    if proxy:
        launch["proxy"] = build_proxy_option(proxy)
    return pw.chromium.launch(**launch)


def _worker(sess: PaySession, checkout_url: str, proxy: str, cfg: Any,
            client: Any) -> None:
    """会话专属线程：从开浏览器到结束全程在这里跑。"""
    from playwright.sync_api import sync_playwright

    from core.vcc_checkout_pay import (
        AmountGateError, SEL_SUBMIT, VccPayError, check_amount_gate,
        fill_checkout, read_total, _page_error,
    )
    from core.vcc_client import VccError

    out_dir = Path(cfg.VCC_SCREENSHOT_DIR or "cache/vcc_pay")

    # 取卡要走白名单 IP，不能走账号代理，所以在开浏览器之前做
    try:
        card_row = client.pick_card(card_id=(cfg.VCC_CARD_ID or "").strip(),
                                    min_balance=cfg.VCC_MIN_CARD_BALANCE)
        card_id = str(card_row.get("cardId") or "")
        _touch(sess, card_mask=str(card_row.get("cardNoMask") or ""))
        logger.info("[pay-session] %s 选用卡 %s（%s）", sess.sid, card_id, sess.card_mask)

        if (cfg.VCC_NAME_SOURCE or "holder").lower() == "config":
            name = (cfg.VCC_BILLING_NAME or "").strip()
            if not name:
                raise VccPayError("VCC_NAME_SOURCE=config 但 VCC_BILLING_NAME 为空")
        else:
            holder = client.pick_cardholder(int(cfg.VCC_HOLDER_ID or 0))
            name = str(holder.get("holderName") or "").strip()
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

        card = client.card_secrets(card_id)   # 敏感明文，禁止写日志
    except (VccError, VccPayError) as exc:
        _touch(sess, state=ST_FAILED, message=str(exc))
        logger.error("[pay-session] %s 准备失败：%s", sess.sid, exc)
        return

    # CloakBrowser 自己管理 Playwright 实例，不能套在 sync_playwright() 里，
    # 否则报 "Sync API inside the asyncio loop"。所以先独立试它，失败再开裸的。
    browser = _try_cloak_browser(cfg, proxy, sess)
    pw_ctx = None
    if browser is None:
        pw_ctx = sync_playwright().start()
        browser = _plain_browser(pw_ctx, cfg, proxy)
        logger.info("[pay-session] %s 使用裸 Playwright", sess.sid)

    context = None
    try:
        if proxy:
            logger.info("[pay-session] %s 浏览器走代理", sess.sid)
        context = browser.new_context(viewport={"width": VIEWPORT_W,
                                                "height": VIEWPORT_H})
        page = context.new_page()
        page.set_default_timeout(int(cfg.VCC_PAGE_TIMEOUT * 1000))
        page.goto(checkout_url, wait_until="domcontentloaded")

        from core.vcc_checkout_pay import PayResult
        tmp = PayResult()
        fill_checkout(page, card=card, email=sess.email, billing=billing,
                      cfg=cfg, result=tmp, out_dir=out_dir)

        # 先等税费重算稳定，再读金额：地址决定税，重算期间读到的是中间值。
        _wait_total_rendered(page)
        currency, amount = read_total(page)
        expected = float(cfg.VCC_EXPECTED_AMOUNT or 0.0)
        _touch(sess, currency=currency, amount=amount, expected=expected)

        gate_error = ""
        try:
            check_amount_gate(
                amount, currency,
                expected=expected,
                expected_currency=(cfg.VCC_EXPECTED_CURRENCY or "").strip(),
                tolerance=float(cfg.VCC_AMOUNT_TOLERANCE or 0.0),
                max_charge=float(cfg.VCC_MAX_CHARGE or 0.0))
        except AmountGateError as exc:
            gate_error = str(exc)
            logger.error("[pay-session] %s 闸门未通过：%s", sess.sid, exc)

        _shoot(page, sess)
        shown = f"{currency or '?'} {amount:.2f}" if amount is not None else "未知"
        _touch(sess, state=ST_AWAITING_CONFIRM, gate_error=gate_error,
               message=(f"表单已填好，应付 {shown}，等待确认"
                        if not gate_error else f"金额闸门未通过：{gate_error}"))

        _wait_and_submit(page, sess, cfg, client, card_id, out_dir,
                         currency, amount, SEL_SUBMIT, _page_error)
    except Exception as exc:  # noqa: BLE001 - 汇总成状态而非上抛
        if sess.state not in _TERMINAL:
            _touch(sess, state=ST_FAILED,
                   message=f"{type(exc).__name__}: {exc}")
        logger.exception("[pay-session] %s 异常", sess.sid)
    finally:
        for closer in (context, browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        if pw_ctx is not None:
            try:
                pw_ctx.stop()
            except Exception:  # noqa: BLE001
                pass


def _wait_total_rendered(page, timeout: float = 12.0) -> None:
    """等订单金额稳定下来再截图。

    只检查"有没有数字"不够：填地址前页面就显示未免税的总额（例如 1311），
    之后税重算期间该区块会变回骨架占位块。所以要等连续两次读数相同，
    才说明重算结束、人看到的就是最终要付的数。
    """
    from core.vcc_checkout_pay import SEL_TOTAL, parse_amount

    deadline = time.time() + timeout
    last = None
    stable = 0
    while time.time() < deadline:
        try:
            text = (page.inner_text(SEL_TOTAL, timeout=1000) or "").strip()
        except Exception:  # noqa: BLE001 - 骨架期间选择器可能不存在
            text = ""
        _, value = parse_amount(text) if text else ("", None)
        if value is not None and value == last:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last = value
        try:
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            return
    logger.debug("[pay-session] 金额在 %.0fs 内未稳定，按现状截图", timeout)


def _wait_and_submit(page, sess, cfg, client, card_id, out_dir,
                     currency, amount, sel_submit, page_error) -> None:
    """等人确认，确认后提交并处理 3DS。"""
    timeout = float(getattr(cfg, "VCC_SESSION_TIMEOUT", 900.0) or 900.0)
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            cmd = sess._cmd.get(timeout=1.0)
        except queue.Empty:
            continue

        if cmd.get("op") == "cancel":
            _touch(sess, state=ST_CANCELLED, message="已取消，未提交")
            return

        if cmd.get("op") != "confirm":
            continue

        # 二次校验：确认的必须就是看到的那个数，防止页面在等待期间变了
        if sess.gate_error:
            _touch(sess, message=f"闸门未通过，拒绝提交：{sess.gate_error}")
            return
        now_cur, now_amt = read_total_safe(page)
        if now_amt is not None and amount is not None and \
                abs(now_amt - amount) > 0.01:
            _touch(sess, state=ST_FAILED,
                   message=f"页面金额已变化（确认时 {amount}，现在 {now_amt}），已拒绝提交")
            _shoot(page, sess)
            return

        _touch(sess, state=ST_SUBMITTING, message="正在提交…")
        logger.warning("[pay-session] %s 人工确认，提交支付 %s %s",
                       sess.sid, currency, amount)
        page.click(sel_submit)
        _touch(sess, submitted=True)

        # Stripe 的 Processing 可能持续几分钟，固定等几秒就判定会误判。
        submit_timeout = float(getattr(cfg, "VCC_SUBMIT_TIMEOUT", 180.0) or 180.0)
        outcome = _wait_settled(page, sess, timeout=submit_timeout)
        _shoot(page, sess)

        # 人机验证可能反复出现，过一次继续等页面落定
        rounds = 0
        while outcome == "captcha" and rounds < 3:
            rounds += 1
            if not getattr(cfg, "VCC_REMOTE_CONTROL", True):
                _touch(sess, state=ST_FAILED,
                       message="页面要求人机验证，但远程交互未开启，无法完成")
                return
            got = _human_takeover(
                page, sess, cfg,
                "页面要求人机验证，请在下方画面中直接点击完成（点完会自动继续）")
            if got != "resolved":
                return
            _touch(sess, state=ST_SUBMITTING, message="验证已完成，继续等待结果…")
            outcome = _wait_settled(page, sess, timeout=submit_timeout)
            _shoot(page, sess)

        if outcome == "challenge" and getattr(cfg, "VCC_3DS_ENABLED", True):
            _handle_3ds_interactive(page, sess, cfg, client, card_id)
            try:
                page.wait_for_timeout(5000)
            except Exception:  # noqa: BLE001
                pass
            _shoot(page, sess)
        elif outcome == "processing":
            _touch(sess, state=ST_FAILED,
                   message="提交后页面长时间停在处理中，结果未知；"
                           "请到 VCC 后台核对该卡是否产生扣款，不要重复提交")
            return

        err = page_error(page)
        if err:
            _touch(sess, state=ST_FAILED, message=f"提交后页面报错：{err}")
        else:
            shown = f"{currency} {amount:.2f}" if amount is not None else ""
            _touch(sess, state=ST_DONE, message=f"已提交支付 {shown}".strip())
        return

    if sess.state not in _TERMINAL:
        _touch(sess, state=ST_FAILED, message=f"等待确认超时（{int(timeout)}秒），未提交")


def read_total_safe(page) -> tuple[str, float | None]:
    from core.vcc_checkout_pay import read_total
    try:
        return read_total(page)
    except Exception:  # noqa: BLE001
        return "", None


def _detect_captcha(page) -> bool:
    """页面是否在等人过人机验证。

    hCaptcha 的答案绑定在解题的那个浏览器上（鼠标轨迹、指纹、IP），
    没法在别处解完把 token 搬过来，所以只能让人直接操作这个浏览器。
    """
    for frame in page.frames:
        url = (frame.url or "").lower()
        if "hcaptcha.com" not in url and "recaptcha" not in url:
            continue
        for selector in ("#checkbox", 'div[role="checkbox"]',
                         ".recaptcha-checkbox", "body"):
            try:
                if frame.locator(selector).first.is_visible(timeout=800):
                    return True
            except Exception:  # noqa: BLE001 - frame 可能随时被销毁
                continue
    # 兜底：Stripe 弹的提示文案
    for text in ("One more step before you're done", "I am human",
                 "確認してください"):
        try:
            if page.get_by_text(text, exact=False).first.is_visible(timeout=600):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _apply_input(page, cmd: dict) -> None:
    """把人在网页上的操作转发给服务端浏览器。"""
    kind = cmd.get("kind")
    try:
        if kind == "click":
            page.mouse.click(float(cmd.get("x", 0)), float(cmd.get("y", 0)))
        elif kind == "move":
            page.mouse.move(float(cmd.get("x", 0)), float(cmd.get("y", 0)))
        elif kind == "down":
            page.mouse.move(float(cmd.get("x", 0)), float(cmd.get("y", 0)))
            page.mouse.down()
        elif kind == "up":
            page.mouse.move(float(cmd.get("x", 0)), float(cmd.get("y", 0)))
            page.mouse.up()
        elif kind == "scroll":
            page.mouse.wheel(float(cmd.get("dx", 0)), float(cmd.get("dy", 0)))
        elif kind == "key":
            key = str(cmd.get("key") or "")
            if key:
                page.keyboard.press(key)
        elif kind == "text":
            text = str(cmd.get("text") or "")
            if text:
                page.keyboard.type(text)
    except Exception as exc:  # noqa: BLE001 - 单次操作失败不该中断接管
        logger.debug("[pay-session] 转发操作失败 %s：%s", kind, exc)


def _human_takeover(page, sess, cfg, reason: str) -> str:
    """把页面交给人直接操作，直到人点完成或超时。

    返回 'resolved' / 'cancelled' / 'timeout'。
    """
    _touch(sess, state=ST_AWAITING_HUMAN, message=reason)
    logger.warning("[pay-session] %s 需要人工接管：%s", sess.sid, reason)

    interval = float(getattr(cfg, "VCC_FRAME_INTERVAL", 0.6) or 0.6)
    deadline = time.time() + float(
        getattr(cfg, "VCC_SESSION_TIMEOUT", 900.0) or 900.0)
    last_shot = 0.0

    while time.time() < deadline:
        # 先处理堆积的输入，保证点击跟手
        drained = 0
        while drained < 50:
            try:
                cmd = sess._cmd.get_nowait()
            except queue.Empty:
                break
            drained += 1
            op = cmd.get("op")
            if op == "cancel":
                _touch(sess, state=ST_CANCELLED, message="人工接管期间被取消")
                return "cancelled"
            if op == "done":
                return "resolved"
            if op == "input":
                _apply_input(page, cmd)

        now = time.time()
        if now - last_shot >= interval:
            _shoot(page, sess, full=False)
            last_shot = now
            # 验证码消失就自动继续，不必等人点完成
            try:
                if not _detect_captcha(page):
                    logger.info("[pay-session] %s 人机验证已通过", sess.sid)
                    return "resolved"
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.1)

    _touch(sess, state=ST_FAILED, message="等待人工操作超时")
    return "timeout"


def _find_3ds_frame(page):
    """找真正的 3DS 质询 frame。

    不能只看 frame 名字或 URL：Stripe 提交后会加载名字带 3ds 的隐藏容器用于
    预热，页面其实停在 Processing，并没有要人输码。真质询的标志是 frame 里
    有可见的输入框，所以这里以此为准。
    """
    for frame in page.frames:
        url = (frame.url or "").lower()
        name = (frame.name or "").lower()
        if not ("challenge" in url or "3ds" in name or "3ds" in url
                or "acs" in url or "authenticate" in url):
            continue
        for selector in ('input[autocomplete="one-time-code"]',
                         'input[type="tel"]', 'input[type="text"]',
                         'input[type="password"]', "input"):
            try:
                box = frame.locator(selector).first
                if box.is_visible(timeout=1200):
                    return frame, selector
            except Exception:  # noqa: BLE001 - frame 可能随时被销毁
                continue
    return None, ""


def _wait_settled(page, sess, timeout: float = 120.0) -> str:
    """提交后等页面落定。

    返回 'challenge'（3DS）/ 'captcha'（人机验证）/ 'left'（已离开表单）/
    'processing'（超时仍未落定）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame, _ = _find_3ds_frame(page)
        if frame is not None:
            return "challenge"
        try:
            if _detect_captcha(page):
                return "captcha"
        except Exception:  # noqa: BLE001
            pass
        try:
            from core.vcc_checkout_pay import SEL_SUBMIT
            if page.query_selector(SEL_SUBMIT) is None:
                return "left"
            label = (page.inner_text(SEL_SUBMIT, timeout=1200) or "").strip().lower()
            if label and "processing" not in label and "処理" not in label:
                # 按钮回到可点状态，说明这一轮提交已经结束（通常是被拒）
                return "left"
        except Exception:  # noqa: BLE001
            return "left"
        try:
            page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            return "left"
    return "processing"


def _handle_3ds_interactive(page, sess, cfg, client, card_id) -> None:
    """3DS：先轮询 VCC 自动取码；取不到就截图交人工输入。"""
    started = time.time()
    try:
        page.wait_for_timeout(4000)
        frame, input_sel = _find_3ds_frame(page)
        if frame is None:
            return
        logger.info("[pay-session] %s 检测到 3DS 质询", sess.sid)
        _shoot(page, sess)

        code = ""
        try:
            code = client.wait_3ds_code(
                card_id, timeout=float(cfg.VCC_3DS_TIMEOUT),
                interval=float(cfg.VCC_3DS_POLL_INTERVAL), since=started)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[pay-session] 轮询验证码失败：%s", exc)

        if not code:
            # 兜底：让人看截图手输
            _touch(sess, state=ST_AWAITING_OTP,
                   message="未能自动取到 3DS 验证码，请查看截图后手动输入")
            deadline = time.time() + float(
                getattr(cfg, "VCC_SESSION_TIMEOUT", 900.0) or 900.0)
            while time.time() < deadline:
                try:
                    cmd = sess._cmd.get(timeout=1.0)
                except queue.Empty:
                    continue
                if cmd.get("op") == "cancel":
                    _touch(sess, state=ST_CANCELLED, message="3DS 阶段被取消")
                    return
                if cmd.get("op") == "otp":
                    code = str(cmd.get("code") or "").strip()
                    if code:
                        break
            if not code:
                _touch(sess, state=ST_FAILED, message="等待人工输入 3DS 验证码超时")
                return
            _touch(sess, state=ST_SUBMITTING, message="已收到验证码，正在提交…")

        # frame 可能在等待期间被重建，重新找一次
        live, live_sel = _find_3ds_frame(page)
        frame = live or frame
        input_sel = live_sel or input_sel
        try:
            frame.fill(input_sel, code)
        except Exception as exc:  # noqa: BLE001
            _touch(sess, message=f"验证码已取到但填入失败：{type(exc).__name__}")
            return
        for selector in ('button[type="submit"]', "button"):
            try:
                frame.click(selector)
                break
            except Exception:  # noqa: BLE001
                continue
        logger.info("[pay-session] %s 3DS 验证码已提交", sess.sid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[pay-session] 3DS 处理异常：%s", exc)


def start_session(*, account_id: int, email: str, checkout_url: str,
                  proxy: str = "", cfg: Any = None,
                  client: Any = None) -> PaySession:
    """开一个支付会话。立即返回，填表在后台线程进行。"""
    if cfg is None:
        from config import vcc as cfg  # type: ignore[no-redef]
    from core.vcc_client import build_client_from_config

    _reap()
    with _lock:
        live = [s for s in _sessions.values() if s.state not in _TERMINAL]
        if live:
            raise RuntimeError(
                f"已有进行中的支付会话（{live[0].sid}，状态 {live[0].state}），"
                "请先完成或取消")

        sess = PaySession(sid=uuid.uuid4().hex[:12], account_id=account_id,
                          email=email)
        _sessions[sess.sid] = sess

    client = client or build_client_from_config()
    thread = threading.Thread(
        target=_worker, args=(sess, checkout_url, proxy, cfg, client),
        name=f"pay-{sess.sid}", daemon=True)
    sess._thread = thread
    thread.start()
    return sess


def get_session(sid: str) -> PaySession | None:
    _reap()
    return _sessions.get(sid)


def list_active() -> PaySession | None:
    """返回当前进行中的会话。同时只允许一个，所以最多一个。"""
    _reap()
    with _lock:
        for sess in _sessions.values():
            if sess.state not in _TERMINAL:
                return sess
    return None


def last_finished(max_age: float = 600.0) -> PaySession | None:
    """最近结束的会话，用于页面重开时还能看到上一单的结果。"""
    _reap()
    now = time.time()
    with _lock:
        done = [s for s in _sessions.values()
                if s.state in _TERMINAL and now - s.updated_at <= max_age]
    return max(done, key=lambda s: s.updated_at) if done else None


def send_command(sid: str, op: str, **kw) -> bool:
    sess = _sessions.get(sid)
    if not sess or sess.state in _TERMINAL:
        return False
    sess._cmd.put({"op": op, **kw})
    return True


def _reap(max_age: float = 3600.0) -> None:
    """清掉已结束且超过保留期的会话，避免截图把内存吃满。"""
    now = time.time()
    with _lock:
        for sid in [s for s, v in _sessions.items()
                    if v.state in _TERMINAL and now - v.updated_at > max_age]:
            _sessions.pop(sid, None)
