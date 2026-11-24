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
import re
import threading
import time
import urllib.parse
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
    # 这一单用的卡的可读标签，支付成功后落到账号上备查：
    #   VCC 托管卡  -> VCC-1771（VCC- 前缀 + 卡号后四位）
    #   自有卡池卡  -> 卡池里填的备注名（label），没填才退回 VCC- 格式
    # 以前只有全局 pay_last_card_id（每付一单就被覆盖），事后查不到
    # 某个账号是哪张卡付的——续费、对账、某张卡被风控时都要用。
    card_label: str = ""
    gate_error: str = ""          # 闸门未通过的原因；非空时禁止确认
    promo_code: str = ""          # 本单用掉的优惠码，支付成功后登记进池备查
    shot_b64: str = ""            # 最新截图，base64，不落盘
    submitted: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # 登录用的出口 IP 池（首选在前）+ 池子耗尽时的重拉回调。
    # 风控 403 是 IP 级的，多轮登录各换一个出口才有意义；手动订阅
    # 不传就是单出口，行为与从前一致。
    _proxies: list[str] = field(default_factory=list, repr=False)
    _refetch: Any = field(default=None, repr=False)
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
            "card_label": self.card_label,
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

# 等待中的"支付后查套餐"：account_id -> 到期时间戳。
# 这些任务跑在 daemon 线程里，重启会直接丢掉；登记在这里让
# /api/busy 看得见，重启前能拦住。
_PENDING_PLAN_CHECKS: dict[int, float] = {}
_plan_check_lock = threading.Lock()


def _touch(sess: PaySession, **kw) -> None:
    for key, value in kw.items():
        setattr(sess, key, value)
    sess.updated_at = time.time()


def _record_promo(sess: PaySession) -> None:
    """把这一单用掉的优惠码登记进池。

    只在支付真正提交成功后调用：手动填的码不经过优惠码池分派，用完就查不到
    记录了。登记失败不能影响支付结果，所以这里吞掉异常只记日志。
    """
    code = str(getattr(sess, "promo_code", "") or "").strip()
    if not code:
        return
    try:
        from core.team_checkout_service import record_used_promo_code

        res = record_used_promo_code(
            code, account_id=int(sess.account_id or 0) or None,
            account_email=str(sess.email or "") or None)
        if res.get("ok"):
            logger.info("[pay-session] %s 优惠码已登记：%s（%s）",
                        sess.sid, res.get("code"), res.get("action"))
        else:
            logger.warning("[pay-session] %s 优惠码登记被拒：%s",
                           sess.sid, res.get("error"))
    except Exception:  # noqa: BLE001 - 登记失败不能影响已完成的支付
        logger.exception("[pay-session] %s 优惠码登记失败", sess.sid)


def _record_paid_at(sess: PaySession) -> None:
    """记下支付成功的时间和用的哪张卡。

    编号不在这里分配：此刻还不知道买到的是 5h 还是周限额，那要等
    30 秒后的套餐查询回来才知道。所以这里只落时间，编号由
    db.assign_subscription_no 在限额类型确定后再给。

    卡标签也在这里落库。以前只有一个全局 pay_last_card_id，每付一单
    就被覆盖，事后根本查不到某个账号是哪张卡付的——续费扣款、对账、
    某张卡被风控时想知道牵连哪些号，都需要这个。
    """
    account_id = int(sess.account_id or 0)
    if account_id <= 0:
        return
    try:
        from core import db
        db.set_account_paid_at(account_id)
        logger.info("[pay-session] %s 已记录支付时间：账号 #%s", sess.sid, account_id)
    except Exception:  # noqa: BLE001 - 记录失败不能影响已完成的支付
        logger.exception("[pay-session] %s 记录支付时间失败", sess.sid)

    label = str(getattr(sess, "card_label", "") or "").strip()
    if not label:
        return
    try:
        from core import db
        db.set_account_pay_card(account_id, label)
        logger.info("[pay-session] %s 已记录支付卡片：账号 #%s 用 %s",
                    sess.sid, account_id, label)
    except Exception:  # noqa: BLE001 - 同上，记录失败不能影响支付结果
        logger.exception("[pay-session] %s 记录支付卡片失败", sess.sid)


def _clear_checkout_link(sess: PaySession) -> None:
    """支付成功后清掉长链。

    长链付过一次就作废。留着的话下次点支付会提示"已有长链，可复用"，
    拿它再付必然失败，还白等一轮探测。
    """
    account_id = int(sess.account_id or 0)
    if account_id <= 0:
        return
    try:
        from core import db
        db.update_account_team_checkout(account_id, clear=True)
        logger.info("[pay-session] %s 已清掉用过的长链：账号 #%s",
                    sess.sid, account_id)
    except Exception:  # noqa: BLE001 - 清理失败不能影响已完成的支付
        logger.exception("[pay-session] %s 清长链失败", sess.sid)


def _schedule_plan_check(sess: PaySession, cfg: Any) -> None:
    """提交后延迟轮询套餐，确认订阅真的生效；不生效则回滚登记。

    提交后页面无报错不等于支付成功（2026-11-19 #150：填卡 iframe 异常
    导致提交没生效，页面无报错文本，14 秒就被误判成功，paid_at/优惠码
    全部错误落库）。所以这里的轮询才是最终裁决：
      - plan 变成付费套餐（team/plus/pro/go）→ 确认成功
      - 连续 POLL_ROUNDS 轮仍 free → 判定未生效，回滚 paid_at/卡标签/
        优惠码，并把会话翻成 failed（钱扣没扣以 VCC 卡交易为准，人工核对）

    用独立线程延时，不占用支付线程，也不让前端等。
    等待期间会登记进 _PENDING_PLAN_CHECKS，让 /api/busy 看得见——
    2026-11-18 线上：#94 付款后 7 秒我重启了服务，safe-restart.sh 报
    "当前空闲"（延迟线程不在任何统计里），线程被杀，套餐查询从未执行，
    订阅编号也就一直空着。重启窗口必须知道这件事还没做完。
    """
    if not bool(getattr(cfg, "VCC_AUTO_PLAN_CHECK", True)):
        return
    try:
        delay = max(0.0, float(getattr(cfg, "VCC_AUTO_PLAN_CHECK_DELAY", 30.0) or 0.0))
    except (TypeError, ValueError):
        delay = 30.0

    account_id = int(sess.account_id or 0)
    if account_id <= 0:
        return

    with _plan_check_lock:
        _PENDING_PLAN_CHECKS[account_id] = time.time() + delay

    def _run() -> None:
        try:
            if delay:
                time.sleep(delay)
            try:
                effective = _verify_payment_effective(sess, account_id)
            except Exception as exc:  # noqa: BLE001 - 确认失败不炸线程，按未生效保守回滚
                logger.warning("[pay-session] %s 支付确认异常（按未生效回滚）：%s: %s",
                               sess.sid, type(exc).__name__, str(exc)[:160])
                effective = False
            if not effective:
                _rollback_payment_records(sess)
            else:
                logger.info("[pay-session] %s 支付确认完成：账号 #%s 套餐已生效",
                            sess.sid, account_id)
        finally:
            with _plan_check_lock:
                _PENDING_PLAN_CHECKS.pop(account_id, None)

    threading.Thread(target=_run, name=f"plan-after-pay-{account_id}",
                     daemon=True).start()
    logger.info("[pay-session] %s 已提交，%.0fs 后开始套餐确认", sess.sid, delay)


_VERIFY_POLL_ROUNDS = 4       # 套餐确认轮数；OpenAI 侧写套餐有延迟，一次 free 不定论
_VERIFY_POLL_GAP = 45.0       # 每轮间隔（秒），总窗口 ≈ 3.5 分钟


def _verify_payment_effective(sess: PaySession, account_id: int) -> bool:
    """轮询套餐直到生效或轮数耗尽。生效返回 True。

    每轮：入队一次套餐查询（服务内部带限速槽），等 POLL_GAP 秒后读
    账号当前套餐。付费套餐（非 free）即认定生效。
    """
    from core import db
    from core.plan_check_service import enqueue_account_plan_check

    for attempt in range(1, _VERIFY_POLL_ROUNDS + 1):
        acc = db.get_account(account_id)
        if not acc:
            logger.warning("[pay-session] %s 套餐确认跳过：账号 %s 不存在", sess.sid, account_id)
            return False
        token = str(acc.get("access_token") or "").strip()
        if not token:
            logger.warning("[pay-session] %s 套餐确认跳过：账号 %s 没有 access_token",
                           sess.sid, account_id)
            return False
        res = enqueue_account_plan_check(
            account_id=account_id,
            email=str(acc.get("email") or ""),
            access_token=token,
            trigger="payment_auto",
        )
        if res.get("accepted"):
            logger.info("[pay-session] %s 套餐确认第 %d/%d 轮已入队：%s",
                        sess.sid, attempt, _VERIFY_POLL_ROUNDS, acc.get("email"))
        else:
            logger.warning("[pay-session] %s 套餐确认第 %d 轮未入队：%s",
                           sess.sid, attempt, res.get("error") or res)
        time.sleep(_VERIFY_POLL_GAP)
        acc = db.get_account(account_id) or {}
        plan = str(acc.get("current_plan_type") or acc.get("plan_type") or "").lower()
        if plan and plan != "free":
            return True
        logger.warning("[pay-session] %s 套餐确认第 %d/%d 轮后仍为 %s",
                       sess.sid, attempt, _VERIFY_POLL_ROUNDS, plan or "未知")
    return False


def _rollback_payment_records(sess: PaySession) -> None:
    """套餐确认失败：回滚 paid_at/卡标签/优惠码，会话翻成 failed。

    只回滚登记，不动 VCC 卡——钱扣没扣以卡交易流水为准，人工核对。
    """
    account_id = int(sess.account_id or 0)
    logger.error("[pay-session] %s 支付疑似未生效：账号 #%s 连续 %d 轮套餐仍 free，"
                 "回滚支付登记（paid_at/卡标签/优惠码）；请到 VCC 后台核对卡交易",
                 sess.sid, account_id, _VERIFY_POLL_ROUNDS)
    try:
        from core import db
        db.clear_account_payment(account_id)
    except Exception:  # noqa: BLE001
        logger.exception("[pay-session] %s 回滚 paid_at 失败", sess.sid)
    code = str(getattr(sess, "promo_code", "") or "").strip()
    if code:
        try:
            from core.team_checkout_service import reset_promo_code_by_code
            if reset_promo_code_by_code(code):
                logger.info("[pay-session] %s 优惠码 %s 已回滚为 unused", sess.sid, code)
        except Exception:  # noqa: BLE001
            logger.exception("[pay-session] %s 回滚优惠码失败", sess.sid)
    if sess.state == ST_DONE:
        _touch(sess, state=ST_FAILED,
               message="页面报成功但套餐未生效（多轮确认仍 free），已回滚登记；"
                       "请核对卡交易后重试")


def pending_plan_checks() -> list[dict]:
    """还在等待的"支付后查套餐"任务。

    给 /api/busy 用：这些任务跑在 daemon 线程里，重启会直接丢掉，
    而丢掉的后果是账号付了款却没有套餐信息和订阅编号。
    """
    now = time.time()
    with _plan_check_lock:
        items = list(_PENDING_PLAN_CHECKS.items())
    return [{"account_id": aid, "due_in": round(max(0.0, due - now), 1)}
            for aid, due in items]


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
        from config import cloakbrowser as cloak_cfg
        key = str(getattr(cloak_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
        if key:
            opts["license_key"] = key
    except Exception as exc:  # noqa: BLE001 - 读不到配置就按免费 binary 跑
        logger.warning("[pay-session] %s 读取 CloakBrowser license 失败：%s",
                       sess.sid, exc)

    try:
        browser = cloak_launch(**opts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[pay-session] %s CloakBrowser 启动失败，退回裸 Playwright：%s",
                       sess.sid, str(exc)[:160])
        return None
    logger.info("[pay-session] %s 使用 CloakBrowser（humanize=%s geoip=%s headless=%s）",
                sess.sid, opts["humanize"], opts["geoip"], opts["headless"])
    return browser


def _login_cookies_for(email: str, proxies, *, rounds: int = 6,
                       gap: float = 30.0, refetch=None,
                       used_proxy_out: list[str] | None = None) -> list[dict]:
    """登录账号并取回可注入浏览器的 chatgpt.com 会话 cookie。

    单席位结账页在 chatgpt.com 自己域下，必须有登录态才打得开，
    否则直接 302 到 /auth/login。而协议会话靠的是 Bearer header，
    光预热拿不到 session-token，必须真走完密码 + TOTP 登录。

    风控 403 是出口 IP 级的：2026-11-19 实测同一出口 4 轮全 403，
    换设备指纹没用，等 3 分钟也没等来恢复。所以失败一轮就换下一个
    出口 IP；池子耗尽且给了 refetch 回调时再拉一批（已试过的 IP 不
    重试）。总轮数有上限，避免无限烧时间。

    used_proxy_out：可选的输出列表，登录成功时把实际跑通的出口追加进去，
    方便调用方让浏览器切换到同一个活出口（避免登录换了活口但浏览器仍死绑
    初始坏出口，2026-11-24 18:00 #171 事故）。
    """
    import time as _t

    from core import db
    from core.account_liveness import (_login_via_password_or_otp,
                                       _network_preflight_with_retry)
    from core.openai_auth import follow_authorize
    from core.session import close_browser_session

    # 兼容旧调用：单个地址字符串也当成一元池子
    pool = ([proxies] if isinstance(proxies, str)
            else [str(p or "").strip() for p in (proxies or [])
                  if str(p or "").strip()])
    tried: set[str] = set()
    acc = db.get_account_by_email(email) or {}
    src = str(acc.get("email_source") or "")

    for rd in range(1, int(rounds) + 1):
        if not pool:
            if refetch is None:
                break
            logger.info("[pay-session] 出口池已耗尽，重拉一批 1024proxy 出口")
            try:
                pool = [p for p in (refetch() or []) if p and p not in tried]
            except Exception:  # noqa: BLE001 - 重拉失败按空处理
                logger.warning("[pay-session] 重拉出口失败", exc_info=True)
                pool = []
            if not pool:
                break
        proxy = pool.pop(0)
        tried.add(proxy)
        session = None
        try:
            session, authorize_url = _network_preflight_with_retry(
                email, proxy, fingerprint_state={})
            follow_authorize(session, authorize_url)
            _login_via_password_or_otp(session, email, _t.time(),
                                       email_source=src)
            jar = session.session.cookies.get_dict() or {}
            cookies = [_cookie_for_playwright(k, v) for k, v in jar.items()]
            if any("session-token" in c["name"] for c in cookies):
                if used_proxy_out is not None:
                    used_proxy_out.append(proxy)
                logger.info("[pay-session] 第 %s 轮登录成功（出口 %s），取到 %s 个 cookie",
                            rd, proxy, len(cookies))
                return cookies
            logger.warning("[pay-session] 第 %s 轮登录没拿到 session-token（出口 %s）",
                           rd, proxy)
        except Exception as exc:  # noqa: BLE001 - 换出口重试
            try:
                from core.session import invalidate_geo_cache
                invalidate_geo_cache(proxy)
            except Exception:  # noqa: BLE001
                pass
            logger.warning("[pay-session] 第 %s/%s 轮登录失败（出口 %s）：%s",
                           rd, rounds, proxy, str(exc)[:160])
        finally:
            if session is not None:
                try:
                    close_browser_session(session)
                except Exception:  # noqa: BLE001
                    pass
        if rd < rounds:
            _t.sleep(gap)
    return []


def _cookie_for_playwright(name: str, value: str) -> dict:
    """把 cookie 转成 playwright 认的结构。

    cookie 前缀有硬性规则，不满足 add_cookies 直接报
    Invalid cookie fields，而且会连累整批：
        __Host-   不能带 domain，必须用 url 指定，且 path=/ secure
        __Secure- 必须 secure
    踩过一次：漏了 secure，恰好 session-token 两个分片全被拒，
    结果页面照样跳登录页。
    """
    c: dict[str, Any] = {"name": name, "value": value,
                         "secure": True, "sameSite": "Lax"}
    if name.startswith("__Host-"):
        c["url"] = "https://chatgpt.com/"
    else:
        c["domain"] = ".chatgpt.com"
        c["path"] = "/"
    return c


def _fill_elements_checkout(page, *, card: dict, billing: dict, cfg, result,
                            out_dir, sess) -> None:
    """填写单席位结账页。

    和 classic 的区别：卡号三要素在 Stripe Elements iframe 里，
    账单地址在另一个 iframe（渲染更晚，要滚动才挂上来）。

    账单国家必须改成卡的归属国：页面默认跟着 checkout session 的
    billing_details 走（我们按 EG 建单是为了拿 EGP 定价），
    但卡是美国卡，不改会被发卡行判定地址不符而拒付。
    """
    from core.elements_checkout import (fill_elements_address,
                                        fill_elements_card,
                                        read_seat_quantity)
    from core.vcc_checkout_pay import VccPayError

    result.stage = "fill"
    _touch(sess, message="正在填写卡片信息")

    # Stripe Elements 的 iframe 渲染很不稳定：同一条链接有时十几秒就绪，
    # 有时 60 秒还没挂上输入框。首次等不到就刷新页面重来——重载比干等有效。
    page_timeout = float(getattr(cfg, "VCC_PAGE_TIMEOUT", 60) or 60)
    filled = None
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            filled = fill_elements_card(page, card=card,
                                        timeout_ms=int(page_timeout * 1000))
            break
        except Exception as exc:  # noqa: BLE001 - 重载后再试
            last_exc = exc
            logger.warning("[pay-session] %s 第 %s/3 次填卡失败：%s",
                           sess.sid, attempt, str(exc)[:160])
            if attempt >= 3:
                break
            _touch(sess, message=f"支付表单没加载出来，重试第 {attempt} 次")
            try:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
            except Exception:  # noqa: BLE001 - 重载失败就直接再试一轮
                logger.debug("[pay-session] 重载失败", exc_info=True)

    if filled is None:
        try:
            frames = [str(getattr(f, "url", ""))[:90] for f in page.frames]
            logger.error("[pay-session] %s 填卡失败，当前 %s 个 frame：%s",
                         sess.sid, len(frames), frames)
            logger.error("[pay-session] %s 页面 URL=%s", sess.sid, page.url)
        except Exception:  # noqa: BLE001
            pass
        _screenshot_safe(page, out_dir, "elements_fill_failed", result)
        raise VccPayError(f"填写卡片失败：{last_exc}") from last_exc
    result.filled.update(filled)

    # 账单地址：在另一个 iframe 里，渲染比卡号晚。
    # 卡是美国卡，页面默认国家是建单时的 EG，必须改过来，
    # 否则发卡行会因账单地址不符拒付。
    _touch(sess, message="正在填写账单地址")
    try:
        addr_filled = fill_elements_address(
            page, billing,
            timeout_ms=int(float(getattr(cfg, "VCC_PAGE_TIMEOUT", 60)) * 1000))
        result.filled.update(addr_filled)
    except Exception as exc:  # noqa: BLE001
        _screenshot_safe(page, out_dir, "elements_address_failed", result)
        raise VccPayError(f"填写账单地址失败：{exc}") from exc

    # 席位数直接决定扣多少钱。这条链路是"创建 2 席再改 1 席"，
    # 万一 update 没生效就会按 2 席扣款，所以提交前必须核一次。
    seats = read_seat_quantity(page)
    if seats is not None:
        logger.info("[pay-session] %s 页面席位数 = %s", sess.sid, seats)
        if seats > 1:
            _screenshot_safe(page, out_dir, "elements_seat_mismatch", result)
            raise VccPayError(
                f"页面显示 {seats} 席，不是预期的 1 席，已停止以免多扣款")
    else:
        logger.warning("[pay-session] %s 读不到席位数，继续靠金额闸门兜底",
                       sess.sid)


def _screenshot_safe(page, out_dir, name: str, result) -> None:
    """截图，失败不影响主流程。"""
    try:
        from core.vcc_checkout_pay import _screenshot
        _screenshot(page, out_dir, name, result.screenshots)
    except Exception:  # noqa: BLE001
        logger.debug("[pay-session] 截图 %s 失败", name, exc_info=True)


def _plain_browser(pw, cfg, proxy: str):
    """裸 Playwright 回退路径。

    默认内核是 Playwright 打过补丁的 Chromium，Stripe 和发卡行 3DS 认得出来。
    配 VCC_BROWSER_CHANNEL=chrome 可改用系统装的真 Google Chrome——指纹不如
    CloakBrowser 完整，但内核是真的，有些卡只在真 Chrome 下过得了认证。
    """
    from core.vcc_checkout_pay import build_proxy_option

    launch: dict[str, Any] = {
        "headless": not bool(getattr(cfg, "VCC_SERVER_HEADED", False)),
        # 没有这些，navigator.webdriver 直接为 true，风控一眼看穿
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
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
        from core.custom_card_store import (CustomCardError, get_secrets,
                                            is_custom_id, local_id_of,
                                            mark_used)
        want_card = (cfg.VCC_CARD_ID or "").strip()
        if is_custom_id(want_card):
            # 自定义卡不在 VCC 名下：pick_card / card_secrets 都会报"卡片不存在"，
            # 三要素只能从本地卡池取。余额也无从查起，由卡主自己保证。
            lid = local_id_of(want_card)
            try:
                sec = get_secrets(lid)
            except CustomCardError as exc:
                raise VccPayError(f"自定义卡不可用：{exc}") from exc
            card_id = want_card
            digits = "".join(ch for ch in str(sec["card_number"]) if ch.isdigit())
            # 卡池存 4 位年（2030），但 Stripe 的有效期框是 MM / YY，
            # 填 4 位会被截成 "03 / 20"，回读校验必然不符。统一取后两位。
            yy = "".join(ch for ch in str(sec["exp_year"]) if ch.isdigit())[-2:]
            mm = "".join(ch for ch in str(sec["exp_month"]) if ch.isdigit()).zfill(2)
            card = {
                "number": digits,
                "exp_month": mm,
                "exp_year": yy,
                "cvc": str(sec["cvv"]).strip(),
            }
            mask = f"{digits[:6]}****{digits[-4:]}" if len(digits) > 10 else digits
            # 自有卡优先用卡池备注名；没填才退回 VCC-后四位，保证总有个可读标识
            label = str(sec.get("label") or "").strip()
            if not label:
                label = "VCC-%s" % digits[-4:] if len(digits) >= 4 else "自有卡"
            _touch(sess, card_mask=mask, card_label=label)
            logger.info("[pay-session] %s 选用自定义卡 %s（%s，标签 %s）",
                        sess.sid, card_id, mask, label)
            mark_used(lid)

            if (cfg.VCC_NAME_SOURCE or "holder").lower() == "config":
                name = (cfg.VCC_BILLING_NAME or "").strip()
            else:
                # 自定义卡没有 VCC 持卡人记录，用卡池里填的姓名
                name = str(sec.get("holder_name") or "").strip()
                if not name:
                    name = (cfg.VCC_BILLING_NAME or "").strip()
            if not name:
                raise VccPayError("自定义卡未填持卡人姓名，请在卡池补填或设置 VCC_BILLING_NAME")
        else:
            card_row = client.pick_card(card_id=want_card,
                                        min_balance=cfg.VCC_MIN_CARD_BALANCE)
            card_id = str(card_row.get("cardId") or "")
            vcc_mask = str(card_row.get("cardNoMask") or "")
            # VCC 托管卡统一记 VCC-后四位。掩码形如 537872713****1771
            tail = "".join(ch for ch in vcc_mask if ch.isdigit())[-4:]
            _touch(sess, card_mask=vcc_mask,
                   card_label="VCC-%s" % tail if tail else "VCC")
            logger.info("[pay-session] %s 选用卡 %s（%s，标签 %s）",
                        sess.sid, card_id, sess.card_mask, sess.card_label)

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

        if not is_custom_id(card_id):
            card = client.card_secrets(card_id)   # 敏感明文，禁止写日志

            # 按卡取它登记的账单地址。每张 VCC 卡绑定的地址不同，而配置里
            # 只能写死一份；地址和卡对不上 Stripe 会直接拒付。姓名也用接口
            # 返回的，它和地址同源（VCC 后台的 holderName 可能是另一个名字）。
            if bool(getattr(cfg, "VCC_FETCH_CARD_ADDRESS", True)):
                from core.card_address import CardAddressError, fetch_card_address
                try:
                    fetched = fetch_card_address(card.get("number") or "")
                except CardAddressError as exc:
                    raise VccPayError("取卡账单地址失败：%s" % exc) from exc
                billing = fetched
                name = fetched["name"]
                logger.info("[pay-session] %s 账单地址按卡取得：%s, %s %s %s（%s）",
                            sess.sid, fetched["city"], fetched["state"],
                            fetched["postal"], fetched["country"], name)

        for key in ("country", "city", "line1"):
            if not billing[key]:
                raise VccPayError(f"账单地址缺少必填项：{key}")
    except (VccError, VccPayError) as exc:
        _touch(sess, state=ST_FAILED, message=str(exc))
        logger.error("[pay-session] %s 准备失败：%s", sess.sid, exc)
        return

    browser = None
    pw_ctx = None
    context = None
    try:
        # 单席位结账页在 chatgpt.com 自己域下，没登录态会被 302 到
        # /auth/login，填表逻辑连表单都看不到。2 席的 Stripe 托管页
        # 是匿名可访问的，不需要这步。
        #
        # 先登录、再起浏览器：2026-11-24 18:00 #171 事故中，初始出口 21048
        # 提链后立刻断气，导致先起的浏览器 CloakBrowser GeoIP 超时退回裸内核；
        # 随后登录自动换到活出口 21049 拿到 cookie，但浏览器仍死绑 21048，
        # page.goto 直接 ERR_CONNECTION_CLOSED。先登录拿活出口，再用同一个活出口
        # 起浏览器，登录和支付链路自然一致。
        from core.elements_checkout import is_elements_checkout
        use_elements = is_elements_checkout(checkout_url)
        browser_proxy = proxy
        login_cookies: list[dict] = []
        if use_elements:
            logger.info("[pay-session] %s 单席位结账页，先登录注入 cookie",
                        sess.sid)
            _touch(sess, message="正在登录账号（单席位结账页需要登录态）")
            login_proxy_used: list[str] = []
            login_cookies = _login_cookies_for(
                sess.email,
                getattr(sess, "_proxies", None) or proxy,
                refetch=getattr(sess, "_refetch", None),
                used_proxy_out=login_proxy_used,
            )
            if not login_cookies:
                raise VccPayError(
                    "单席位结账页需要登录态，但多轮登录（已换出口）都没成功"
                    "（多半是该地区出口全被风控）。稍后重试，或改用手动支付")
            if login_proxy_used and login_proxy_used[0]:
                browser_proxy = login_proxy_used[0]
                if browser_proxy != proxy:
                    logger.info(
                        "[pay-session] %s 浏览器切换至登录成功的活出口：%s（原出口 %s）",
                        sess.sid, browser_proxy, proxy or "直连")

        # CloakBrowser 自己管理 Playwright 实例，不能套在 sync_playwright() 里，
        # 否则报 "Sync API inside the asyncio loop"。所以先独立试它，失败再开裸的。
        browser = _try_cloak_browser(cfg, browser_proxy, sess)
        if browser is None:
            pw_ctx = sync_playwright().start()
            browser = _plain_browser(pw_ctx, cfg, browser_proxy)
            logger.info("[pay-session] %s 使用裸 Playwright", sess.sid)

        if browser_proxy:
            logger.info("[pay-session] %s 浏览器走代理", sess.sid)
        context_kwargs: dict[str, Any] = {"viewport": {"width": VIEWPORT_W,
                                                       "height": VIEWPORT_H}}
        if pw_ctx is not None:
            # CloakBrowser 自己按出口 IP 配时区语言；裸路径得自己补，
            # 否则代理在美国而浏览器报 UTC，风控一眼就看出来。
            try:
                from core.cloakbrowser_driver import _build_cloak_locale_options
                loc = _build_cloak_locale_options(browser_proxy or None)
                if loc.get("locale"):
                    context_kwargs["locale"] = loc["locale"]
                if loc.get("timezone"):
                    context_kwargs["timezone_id"] = loc["timezone"]
                if loc.get("accept_language"):
                    context_kwargs["extra_http_headers"] = {
                        "Accept-Language": loc["accept_language"]}
                logger.info("[pay-session] %s 裸浏览器语言时区：locale=%s tz=%s",
                            sess.sid, loc.get("locale") or "默认",
                            loc.get("timezone") or "默认")
            except Exception as exc:  # noqa: BLE001
                logger.debug("[pay-session] 构建语言时区失败：%s", exc)
        context = browser.new_context(**context_kwargs)
        if pw_ctx is not None:
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        if use_elements and login_cookies:
            try:
                context.add_cookies(login_cookies)
            except Exception as exc:  # noqa: BLE001 - 整批失败就逐条来
                logger.warning("[pay-session] cookie 整批注入失败（%s），改逐条",
                               str(exc)[:80])
                okn = 0
                for c in login_cookies:
                    try:
                        context.add_cookies([c])
                        okn += 1
                    except Exception:  # noqa: BLE001
                        logger.debug("[pay-session] cookie %s 注入失败",
                                     c.get("name"))
                if okn < len(login_cookies):
                    logger.warning("[pay-session] cookie 注入 %s/%s",
                                   okn, len(login_cookies))

        page = context.new_page()
        page.set_default_timeout(int(cfg.VCC_PAGE_TIMEOUT * 1000))
        page.goto(checkout_url, wait_until="domcontentloaded")

        if use_elements and "auth/login" in str(page.url or ""):
            raise VccPayError(
                "登录态注入后仍被重定向到登录页，无法填表。"
                "可能 cookie 已失效，请重试或改用手动支付")

        from core.vcc_checkout_pay import PayResult
        tmp = PayResult()
        if use_elements:
            _fill_elements_checkout(page, card=card, billing=billing, cfg=cfg,
                                    result=tmp, out_dir=out_dir, sess=sess)
        else:
            fill_checkout(page, card=card, email=sess.email, billing=billing,
                          cfg=cfg, result=tmp, out_dir=out_dir)

        # 先等税费重算稳定，再读金额：地址决定税，重算期间读到的是中间值。
        if use_elements:
            from core.elements_checkout import read_elements_total
            page.wait_for_timeout(1500)
            # 这个页面只渲染货币符号，没有币种码（实采确认 HTML 里没有，
            # 也没有 __NEXT_DATA__）。$ 可能是 USD/CAD/AUD，解析不出币种
            # 会被闸门当成"无法识别币种"直接拦住。用配置里期望的币种兜底：
            # 它本来就是下单时指定的那个，不是猜的。
            currency, amount = read_elements_total(
                page,
                fallback_currency=(cfg.VCC_EXPECTED_CURRENCY or "").strip())
        else:
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

        # 两种页面的提交按钮不是一个：classic 有 data-testid，
        # 单席位页面只是个普通 button[type=submit]（文案 Subscribe，
        # 但语言跟随出口，不能按文案找）。
        if use_elements:
            from core.elements_checkout import SEL_EL_SUBMIT
            submit_sel = SEL_EL_SUBMIT
        else:
            submit_sel = SEL_SUBMIT
        _wait_and_submit(page, sess, cfg, client, card_id, out_dir,
                         currency, amount, submit_sel, _page_error)
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
    """等人确认，确认后提交并处理 3DS。

    自动订阅场景（VCC_AUTO_CONFIRM=True）不等人：金额闸门已经校验过
    页面总额和币种，再让人点一次没有增加任何判断，只会让流程卡住——
    实测等了 10 分钟后会话就被回收，整单白跑。
    闸门没过时绝不自动提交，仍然停下等人。
    """
    timeout = float(getattr(cfg, "VCC_SESSION_TIMEOUT", 900.0) or 900.0)
    deadline = time.time() + timeout

    if bool(getattr(cfg, "VCC_AUTO_CONFIRM", False)):
        if sess.gate_error:
            logger.error("[pay-session] %s 闸门未通过，不自动提交：%s",
                         sess.sid, sess.gate_error)
            # 自动流程没人会来点确认：闸门拦截的会话若停在 awaiting_confirm，
            # 会一直占着 active 注册表，把本轮后续候选全挡在
            # "已有进行中的支付会话" 上（11-23 makeup-run4 实证连堵 2 个候选）。
            # 闸门没过 = 绝不会扣款，直接标 FAILED（终态），调度器立即换号。
            _touch(sess, state=ST_FAILED,
                   message="金额闸门未通过（未提交）：%s" % sess.gate_error)
        else:
            logger.warning("[pay-session] %s 自动确认（闸门已通过 %s %s）",
                           sess.sid, currency, amount)
            sess._cmd.put({"op": "confirm", "auto": True})

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
        logger.warning("[pay-session] %s %s确认，提交支付 %s %s",
                       sess.sid, "自动" if cmd.get("auto") else "人工",
                       currency, amount)
        page.click(sel_submit)
        _touch(sess, submitted=True)

        # Stripe 的 Processing 可能持续几分钟，固定等几秒就判定会误判。
        submit_timeout = float(getattr(cfg, "VCC_SUBMIT_TIMEOUT", 180.0) or 180.0)
        outcome = _wait_settled(page, sess, timeout=submit_timeout,
                                submit_sel=sel_submit)
        _shoot(page, sess)

        # 人机验证可能反复出现，过一次继续等页面落定
        rounds = 0
        while outcome == "captcha" and rounds < 3:
            rounds += 1
            # 先试自动打码：Stripe 的 Enterprise hCaptcha 能自动过就不用等人。
            # 打码失败（没配 key / 平台失败 / 注入不成功）再回落人工接管，
            # 不让打码成为新的单点故障。
            if _try_solve_captcha(page, sess, cfg):
                logger.info("[pay-session] %s 打码已注入，继续等结果", sess.sid)
                outcome = _wait_settled(page, sess, timeout=submit_timeout,
                                        submit_sel=sel_submit)
                _shoot(page, sess)
                continue
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
            outcome = _wait_settled(page, sess, timeout=submit_timeout,
                                    submit_sel=sel_submit)
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
            # 页面无报错只是消极证据（#150 教训：提交没生效时也没有报错
            # 文本）。先按已提交登记，最终成功与否由套餐确认线程裁决。
            shown = f"{currency} {amount:.2f}" if amount is not None else ""
            _touch(sess, state=ST_DONE, message=f"已提交支付 {shown}，等待套餐确认".strip())
            _record_promo(sess)
            _record_paid_at(sess)
            _clear_checkout_link(sess)
            _schedule_plan_check(sess, cfg)
        return

    if sess.state not in _TERMINAL:
        _touch(sess, state=ST_FAILED, message=f"等待确认超时（{int(timeout)}秒），未提交")


def read_total_safe(page) -> tuple[str, float | None]:
    from core.vcc_checkout_pay import read_total
    try:
        return read_total(page)
    except Exception:  # noqa: BLE001
        return "", None


def _extract_hcaptcha_params(page) -> dict:
    """从当前页面抓出解 hCaptcha 需要的参数。

    必须从**真实会话**里抓，不能照抄常量：Stripe 一次支付会话里同时存在
    好几个 hCaptcha（普通 hCaptcha 和 Enterprise 各一套 sitekey），只有带
    rqdata 的那个才是和本次 SetupIntent/PaymentIntent 绑定的那个。rqdata
    每次都变，抄上一次的必然校验失败。

    返回 {sitekey, rqdata, pageurl, userAgent, found}。
    """
    out = {"sitekey": "", "rqdata": "", "pageurl": "", "userAgent": "", "found": False}
    try:
        out["pageurl"] = str(page.url or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        out["userAgent"] = str(page.evaluate("() => navigator.userAgent") or "")
    except Exception:  # noqa: BLE001
        pass

    # 优先从 Stripe 的 hcaptcha-inner iframe URL 里取：那里的 sitekey 和
    # rqdata 是 Stripe 自己拼上去的，就是当次真正生效的那一份。
    best = None
    for fr in page.frames:
        url = str(getattr(fr, "url", "") or "")
        if "hcaptcha" not in url.lower():
            continue
        parsed = urllib.parse.urlparse(url)
        frag = urllib.parse.parse_qs(parsed.fragment or "")
        query = urllib.parse.parse_qs(parsed.query or "")
        merged = {**query, **frag}
        sitekey = (merged.get("sitekey") or [""])[0]
        rqdata = (merged.get("rqdata") or [""])[0]
        if not sitekey:
            continue
        # 带 rqdata 的那个才是 Enterprise 任务，优先选它
        cand = {"sitekey": sitekey, "rqdata": rqdata}
        if best is None or (rqdata and not best.get("rqdata")):
            best = cand
    if best:
        out.update(best)
        out["found"] = True
        return out

    # 兜底：从页面 HTML 里找 sitekey/rqdata 属性
    try:
        html = page.content() or ""
    except Exception:  # noqa: BLE001
        html = ""
    m = re.search(r'sitekey["\']?\s*[:=]\s*["\']([0-9a-f-]{20,})', html, re.I)
    if m:
        out["sitekey"] = m.group(1)
        out["found"] = True
    m2 = re.search(r'rqdata["\']?\s*[:=]\s*["\']([^"\']{20,})', html, re.I)
    if m2:
        out["rqdata"] = m2.group(1)
    return out


def _inject_hcaptcha_token(page, token: str) -> bool:
    """把打码平台解出的 token 注入页面，让 hCaptcha 认为已通过。

    注入方式按可靠性递进：
      1. 对每个 hCaptcha 表单填 g-recaptcha-response / h-captcha-response
         两个隐藏域（hCaptcha 两者都认），并触发 input/change；
      2. 调页面上的 hcaptcha.setResponse（如果暴露了）；
      3. 点提交按钮让 Stripe 重新发起校验。

    返回是否至少注入成功一处。
    """
    token = str(token or "").strip()
    if not token:
        return False
    js = """
    (token) => {
      let touched = 0;
      const names = ['g-recaptcha-response', 'h-captcha-response'];
      for (const n of names) {
        let els = document.querySelectorAll(`[name="${n}"]`);
        if (!els.length) {
          const ta = document.createElement('textarea');
          ta.name = n; ta.style.display = 'none';
          document.body.appendChild(ta);
          els = document.querySelectorAll(`[name="${n}"]`);
        }
        for (const el of els) {
          el.value = token;
          el.innerHTML = token;
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
          touched++;
        }
      }
      try {
        if (window.hcaptcha && typeof window.hcaptcha.setResponse === 'function') {
          window.hcaptcha.setResponse(token);
          touched++;
        }
      } catch (e) {}
      return touched;
    }
    """
    ok = 0
    for target in [page, *page.frames]:
        try:
            n = target.evaluate(js, token)
            if isinstance(n, (int, float)) and n > 0:
                ok += int(n)
        except Exception:  # noqa: BLE001 - 跨域 frame 会抛，跳过
            continue
    return ok > 0


def _try_solve_captcha(page, sess, cfg) -> bool:
    """尝试用打码平台自动过掉当前的人机验证。成功返回 True。

    失败一律返回 False 而不是抛异常：打码是"锦上添花"的加速路径，
    它挂了也不能把整笔支付带崩，调用方会回落到人工接管。
    """
    if not bool(getattr(cfg, "CAPTCHA_SOLVER_ENABLED", False)):
        return False
    api_key = str(getattr(cfg, "CAPTCHA_2CAPTCHA_KEY", "") or "").strip()
    if not api_key:
        logger.warning("[打码] 已开启但没配 CAPTCHA_2CAPTCHA_KEY，回落人工")
        return False

    from core.captcha_solver import CaptchaError, solve_hcaptcha

    params = _extract_hcaptcha_params(page)
    if not params.get("found") or not params.get("sitekey"):
        logger.warning("[打码] 抓不到 sitekey，无法打码，回落人工")
        return False
    logger.info("[打码] 抓到 sitekey=%s rqdata=%s",
                str(params["sitekey"])[:12], "有" if params.get("rqdata") else "无")

    _touch(sess, state=ST_SUBMITTING, message="正在自动完成人机验证…")
    try:
        token = solve_hcaptcha(
            api_key=api_key,
            sitekey=params["sitekey"],
            pageurl=params["pageurl"],
            user_agent=params.get("userAgent") or "",
            rqdata=params.get("rqdata") or "",
            enterprise=True,
            timeout=float(getattr(cfg, "CAPTCHA_SOLVE_TIMEOUT", 180.0) or 180.0),
            poll_interval=float(getattr(cfg, "CAPTCHA_POLL_INTERVAL", 5.0) or 5.0),
        )
    except CaptchaError as exc:
        logger.warning("[打码] 失败：%s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[打码] 异常：%s: %s", type(exc).__name__, exc)
        return False

    if not _inject_hcaptcha_token(page, token):
        logger.warning("[打码] token 注入失败，回落人工")
        return False

    logger.info("[打码] token 已注入，等页面确认")
    try:
        page.wait_for_timeout(4000)
    except Exception:  # noqa: BLE001
        pass
    # 注入后再点一次提交，让 Stripe 带着新 token 重新校验
    try:
        from core.vcc_checkout_pay import SEL_SUBMIT
        from core.elements_checkout import SEL_EL_SUBMIT
        for sel in (SEL_EL_SUBMIT, SEL_SUBMIT):
            el = page.query_selector(sel)
            if el is not None:
                el.click()
                logger.info("[打码] 已重新提交")
                break
    except Exception:  # noqa: BLE001
        logger.debug("[打码] 重新提交失败", exc_info=True)
    try:
        page.wait_for_timeout(4000)
    except Exception:  # noqa: BLE001
        pass
    return True


def _detect_captcha(page) -> bool:
    """页面是否真的在等人过人机验证。

    2026-11-18 事故：这里原来把 `body` 当成"验证码可见"的判据，而 body 在任何
    iframe 里永远可见。Stripe 结账页会**预加载** hCaptcha iframe（只是挂着，
    并不显示挑战），于是每一笔提交后都被误判成"要求人机验证"，停在
    awaiting_human 干等一个根本不存在的东西。

    实际那一单的真实错误是页面上的 "Payment was not approved"（拒付），
    而当天早些时候成功的 5 笔支付全程没有出现任何验证码。

    现在只认"真的有可见挑战"的信号：
      - hCaptcha 的复选框 / 挑战框（明确的 id、class 或 role）
      - Stripe 的 "One more step before you're done" 等文案
    iframe 自身 visible=False 的一律不算。
    """
    # Stripe 只有在真要人验证时才渲染这段文案，是最可靠的判据
    for text in ("One more step before you're done",
                 "Select the checkbox below",
                 "Verify you are human",
                 "I am human",
                 "確認してください",
                 "请验证您是人类"):
        try:
            if page.get_by_text(text, exact=False).first.is_visible(timeout=600):
                return True
        except Exception:  # noqa: BLE001 - 找不到就继续试下一个
            continue

    # hCaptcha / reCAPTCHA 的挑战元素；不要用 body 这种永远可见的选择器
    challenge_selectors = (
        "#checkbox",                         # hCaptcha 复选框
        ".checkbox",                         # hCaptcha 复选框（旧版）
        "div[role='checkbox']",
        "#challenge-container",              # hCaptcha 挑战容器
        "div.challenge-container",
        ".recaptcha-checkbox-checked",
        ".rc-anchor-normal",                 # reCAPTCHA 面板
    )
    for frame in page.frames:
        url = (frame.url or "").lower()
        if "hcaptcha.com" not in url and "recaptcha" not in url:
            continue
        for selector in challenge_selectors:
            try:
                if frame.locator(selector).first.is_visible(timeout=500):
                    return True
            except Exception:  # noqa: BLE001 - frame 可能随时被销毁
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


def _wait_settled(page, sess, timeout: float = 120.0,
                  submit_sel: str = "") -> str:
    """提交后等页面落定。

    返回 'challenge'（3DS）/ 'captcha'（人机验证）/ 'left'（已离开表单）/
    'processing'（超时仍未落定）。

    submit_sel 必须和当初点下去的那个按钮一致：两种结账页的按钮选择器不同，
    传错会一直查不到元素，直接误判成 'left'（以为提交结束了）。
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
            sel = submit_sel or SEL_SUBMIT
            if page.query_selector(sel) is None:
                return "left"
            label = (page.inner_text(sel, timeout=1200) or "").strip().lower()
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
        # 自定义卡不在 VCC 名下，没有验证码接口可查，轮询只会白等到超时，
        # 直接走人工输入（短信/App 推送在卡主手里）。
        from core.custom_card_store import is_custom_id
        if is_custom_id(card_id):
            logger.info("[pay-session] %s 自定义卡，3DS 需人工输入", sess.sid)
        else:
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
                  client: Any = None, promo_code: str = "",
                  extra_proxies: list[str] | None = None,
                  refetch_proxies: Any = None) -> PaySession:
    """开一个支付会话。立即返回，填表在后台线程进行。

    extra_proxies：登录阶段的备选出口池（proxy 仍是首选，排最前）。
    refetch_proxies：池子耗尽时重拉出口的回调，返回新代理列表；
    两者都不传则登录只用 proxy，与手动订阅路径行为一致。
    """
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
                          email=email,
                          promo_code=str(promo_code or "").strip())
        sess._proxies = [p for p in ([proxy] + list(extra_proxies or []))
                         if str(p or "").strip()]
        sess._refetch = refetch_proxies
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


def last_finished(max_age: float = 3600.0) -> PaySession | None:
    """最近结束的会话，用于页面重开时还能看到上一单的结果。

    保留期与 _reap 一致：原来只留 10 分钟，稍微离开一会儿回来就查不到上一单
    结果了。能不能复用长链不看这里，那只取决于账号存着的长链本身。
    """
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
