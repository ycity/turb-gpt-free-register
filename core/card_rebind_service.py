# -*- coding: utf-8 -*-
"""一键换绑支付卡后台队列。

流程（2026-11-19 实测定型）：
  协议登录取 cookie → CloakBrowser 注入 → admin.openai.com 工作区账单页
  （SSO 状态机穿越）→ 「添加支付方式」Stripe Elements 填卡（复用
  elements_checkout）→ 提交（hCaptcha 阻拦时 2Captcha 自动打码）→
  新卡设默认 → 删旧卡 → 回读校验 → force 覆盖 pay_card_label。

默认换绑目标 = AUTO_SUBSCRIBE_CARD_BLACKLIST 黑名单里的 VCC 牺牲卡
（用户点名拉黑的卡，如 5931/8361）；也可指定卡 ID（VCC id 或 local:N）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import card_rebind as _cfg
from core import db
from core.proxy_utils import mask_proxy_url

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_MAX_WORKERS = 4
_WORKERS = _int_setting("CARD_REBIND_WORKERS", 1, 1, _MAX_WORKERS)
_QUEUE_LIMIT = _int_setting("CARD_REBIND_QUEUE_LIMIT", 50, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="card-rebind")
_RUNNING: set[int] = set()
_OUTSTANDING = 0
_LOCK = threading.Condition()
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def apply_settings() -> dict:
    global _WORKERS, _QUEUE_LIMIT
    workers = _int_setting("CARD_REBIND_WORKERS", 1, 1, _MAX_WORKERS)
    queue_limit = _int_setting("CARD_REBIND_QUEUE_LIMIT", 50, workers, 5000)
    with _LOCK:
        _WORKERS = workers
        _QUEUE_LIMIT = queue_limit
        _LOCK.notify_all()
        return {
            "workers": _WORKERS,
            "queue_limit": _QUEUE_LIMIT,
            "running": len(_RUNNING),
            "outstanding": _OUTSTANDING,
        }


def _release_outstanding() -> None:
    global _OUTSTANDING
    with _LOCK:
        _OUTSTANDING = max(0, _OUTSTANDING - 1)
        _LOCK.notify_all()


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"rebind-{safe}.log"


def is_running(acc_id: int) -> bool:
    with _LOCK:
        return int(acc_id) in _RUNNING


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _normalize_proxy(proxy: str | None) -> str | None:
    text = str(proxy or "").strip()
    if not text:
        return None
    low = text.lower()
    if low.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")):
        return text
    return None


def _target_proxy(proxy: str | None) -> str | None:
    mode = str(getattr(_cfg, "CARD_REBIND_PROXY_MODE", "saved") or "saved").strip().lower()
    if mode not in {"saved", "pool"}:
        raise ValueError(f"CARD_REBIND_PROXY_MODE={mode!r} 无效，可选 saved / pool")
    if mode == "pool":
        return None
    return _normalize_proxy(proxy)


def _blacklist_last4s() -> set[str]:
    try:
        from config import auto_subscribe
        raw = str(getattr(auto_subscribe, "AUTO_SUBSCRIBE_CARD_BLACKLIST", "") or "")
    except Exception:  # noqa: BLE001
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def resolve_target_card(card_id: str = "") -> dict:
    """解析换绑目标卡；缺省取黑名单里第一张可用 VCC 卡。

    返回 {card_id, label, number, exp_month, exp_year(2位), cvv, last4}。
    """
    want = str(card_id or getattr(_cfg, "CARD_REBIND_CARD_ID", "") or "").strip()
    from core.custom_card_store import get_secrets, is_custom_id, local_id_of

    if want and is_custom_id(want):
        s = get_secrets(local_id_of(want))
        label = str(s.get("label") or f"VCC-{s['card_number'][-4:]}")
        return {"card_id": want, "label": label, "number": s["card_number"],
                "exp_month": s["exp_month"], "exp_year": str(s["exp_year"])[-2:],
                "cvv": s["cvv"], "last4": s["card_number"][-4:]}

    from core.vcc_client import build_client_from_config
    client = build_client_from_config()
    cards = client.list_cards() or []
    if want:
        match = next((c for c in cards if str(c.get("cardId") or "") == want), None)
        if not match:
            raise ValueError(f"VCC 里找不到卡 {want}")
        if not client._is_active(match):
            raise ValueError(f"卡 {want} 状态不可用：cardStatus={match.get('cardStatus')}")
    else:
        bl = _blacklist_last4s()
        if not bl:
            raise ValueError("未指定卡且黑名单为空：请在 AUTO_SUBSCRIBE_CARD_BLACKLIST 配置牺牲卡")
        actives = [c for c in cards if client._is_active(c)]
        match = next((c for c in actives if str(c.get("last4") or "") in bl), None)
        if not match:
            raise ValueError(f"黑名单 {sorted(bl)} 里没有可用（ACTIVE）的 VCC 卡")
    card_id = str(match.get("cardId") or "")
    last4 = str(match.get("last4") or str(match.get("cardNoMask") or "")[-4:])
    secrets = client.card_secrets(card_id)
    return {"card_id": card_id, "label": f"VCC-{last4}",
            "number": secrets["number"], "exp_month": secrets["exp_month"],
            "exp_year": str(secrets["exp_year"])[-2:], "cvv": secrets["cvc"],
            "last4": last4}


def _billing_address_for(card_number: str) -> dict:
    """账单地址：卡的关联地址 → ssr.im 按卡号取 → VCC_BILLING 默认。"""
    try:
        from core.billing_address_store import last_address_for_card
        linked = last_address_for_card(card_number)
        if linked:
            return linked
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.card_address import fetch_card_address
        addr = fetch_card_address(card_number)
        if addr:
            return addr
    except Exception as exc:  # noqa: BLE001
        logger.warning("[换绑] 按卡号取地址失败：%s", str(exc)[:120])
    from config import vcc as vcc_cfg
    name = str(getattr(vcc_cfg, "VCC_BILLING_NAME", "") or "")
    if not name:
        raise ValueError("账单地址缺失：卡没有关联地址且 VCC_BILLING_NAME 未配置")
    return {
        "name": name,
        "country": str(getattr(vcc_cfg, "VCC_BILLING_COUNTRY", "") or "US"),
        "state": str(getattr(vcc_cfg, "VCC_BILLING_STATE", "") or ""),
        "city": str(getattr(vcc_cfg, "VCC_BILLING_CITY", "") or ""),
        "line1": str(getattr(vcc_cfg, "VCC_BILLING_LINE1", "") or ""),
        "line2": "",
        "postal": str(getattr(vcc_cfg, "VCC_BILLING_POSTAL", "") or ""),
    }


def _domain_aware_cookies(session) -> list[dict]:
    """从协议会话 jar 提取带域名的 cookie（__Host-/__Secure- 规则）。"""
    out = []
    jar = getattr(getattr(session, "session", None), "cookies", None)
    for c in jar.jar:
        if not c.value:
            continue
        ck = {"name": c.name, "value": c.value, "secure": bool(c.secure), "sameSite": "Lax"}
        if c.name.startswith("__Host-"):
            ck["url"] = f"https://{(c.domain or 'chatgpt.com').lstrip('.')}/"
        else:
            ck["domain"] = c.domain or ".chatgpt.com"
            ck["path"] = c.path or "/"
        out.append(ck)
    return out


def _resolve_workspace_id(acc: dict, token: str, proxy: str | None) -> str:
    """workspace_id：账号字段优先，缺失时用 AT 调 accounts/check 找团队工作区。"""
    ws = str(acc.get("paid_workspace_account_id") or "").strip()
    if ws:
        return ws
    from core.session import BrowserSession
    session = BrowserSession(proxy=_normalize_proxy(proxy), fingerprint_seed="rebind:ws")
    try:
        resp = session.get(
            "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480",
            headers={"authorization": f"Bearer {token}", "accept": "application/json"},
        )
        data = resp.json() or {}
        accounts = data.get("accounts") or {}
        best = ""
        for ws_id, entry in accounts.items():
            ent = (entry or {}).get("entitlement") or {}
            plan = str((entry or {}).get("account", {}).get("plan_type") or "")
            if ent.get("has_active_subscription") or "team" in plan.lower():
                best = ws_id
                break
        if not best:
            raise ValueError("accounts/check 里没有活跃的团队工作区")
        return best
    finally:
        try:
            from core.session import close_browser_session
            close_browser_session(session)
        except Exception:  # noqa: BLE001
            pass


def _make_captcha_solver(ua: str):
    """构造 2Captcha hCaptcha(Enterprise, invisible) 求解回调。"""
    from config import vcc as vcc_cfg
    if not bool(getattr(vcc_cfg, "CAPTCHA_SOLVER_ENABLED", False)):
        return None
    api_key = str(getattr(vcc_cfg, "CAPTCHA_2CAPTCHA_KEY", "") or "").strip()
    if not api_key:
        return None

    def _solve(sitekey: str, pageurl: str) -> str:
        from core.captcha_solver import solve_hcaptcha
        return solve_hcaptcha(
            api_key=api_key, sitekey=sitekey, pageurl=pageurl,
            user_agent=ua, enterprise=True, invisible=True,
            timeout=float(getattr(vcc_cfg, "CAPTCHA_SOLVE_TIMEOUT", 180.0) or 180.0),
            poll_interval=float(getattr(vcc_cfg, "CAPTCHA_POLL_INTERVAL", 5.0) or 5.0),
        )

    return _solve


def _run_card_rebind(*, account_id: int, email: str, card_id: str,
                     proxy: str | None, trigger: str) -> dict:
    global _OUTSTANDING
    fh: logging.FileHandler | None = None
    browser = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    try:
        with _LOCK:
            while len(_RUNNING) >= _WORKERS:
                _LOCK.wait()
            _RUNNING.add(int(account_id))
        if not db.mark_account_card_rebind_running(account_id):
            return {"ok": False, "status": "failed", "error": "账号已删除或换绑状态已被重置"}
        log_file = log_path(email)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("", encoding="utf-8")
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)
        logger.info("[换绑] 开始：email=%s card=%s trigger=%s", email, card_id or "黑名单默认", trigger)

        acc = db.get_account(account_id) or {}
        token = str(acc.get("access_token") or "").strip()
        if not token:
            raise ValueError("账号没有 access_token，无法定位工作区")
        from core.db import _extract_registration_password
        password = _extract_registration_password(acc)
        if not password:
            raise ValueError("账号没有登录密码：换绑走浏览器 SSO 需要密码，请先执行「补密码」")
        totp = bool(acc.get("totp_secret"))
        from core.codex_oauth import _account_totp_code

        target_proxy = _target_proxy(proxy)
        _append_log(email, f"[换绑] 出口：{mask_proxy_url(target_proxy) or '代理池随机'}")

        # 1) 解析目标卡与账单地址
        card = resolve_target_card(card_id)
        billing = _billing_address_for(card["number"])
        _append_log(email, f"[换绑] 目标卡：{card['label']}（尾号 {card['last4']}）")
        logger.info("[换绑] %s 目标卡 %s", email, card["label"])

        # 2) 协议登录取 cookie（密码+TOTP，不依赖邮箱 OTP）
        from core.account_liveness import _login_via_full_web_flow
        from core.session import close_browser_session
        psession, info = _login_via_full_web_flow(
            email, target_proxy, email_source=str(acc.get("email_source") or ""),
            fingerprint_state={})
        try:
            cookies = _domain_aware_cookies(psession)
            profile = getattr(psession, "browser_profile", None) or {}
            ua = str(profile.get("user_agent") or "")
        finally:
            try:
                close_browser_session(psession)
            except Exception:  # noqa: BLE001
                pass
        _append_log(email, f"[换绑] 登录完成，注入 {len(cookies)} 个 cookie")

        # 3) 定位工作区账单页
        ws = _resolve_workspace_id(acc, token, target_proxy)
        ws_name = str(acc.get("paid_workspace_name") or "myteam") or "myteam"
        from core.admin_billing import (
            billing_url, sso_into_admin, wait_billing_section, read_payment_methods,
            open_add_card_form, fill_add_card_form, click_add_submit,
            wait_card_list_change, set_default_card, remove_card,
            solve_and_resubmit, has_hcaptcha_challenge,
        )
        url = billing_url(ws, ws_name)

        # 4) CloakBrowser + cookie 注入 + SSO
        from config import vcc as vcc_cfg
        from core import vcc_pay_session as vps

        class _S:
            sid = f"rebind-{account_id}"

        browser = vps._try_cloak_browser(vcc_cfg, target_proxy, _S())
        if browser is None:
            raise RuntimeError("CloakBrowser 不可用（admin.openai.com 必须真浏览器指纹）")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.set_default_timeout(int(getattr(_cfg, "CARD_REBIND_PAGE_TIMEOUT", 60) * 1000))

        totp_cb = ((lambda: _account_totp_code(email)) if totp else None)
        sso_into_admin(page, email, password=password, totp_code=totp_cb, target_url=url)
        wait_billing_section(page)
        before = read_payment_methods(page)
        _append_log(email, f"[换绑] 当前卡：{json.dumps(before, ensure_ascii=False)}")

        # 5) 加新卡（幂等：已在列表则跳过）
        if any(c["last4"] == card["last4"] for c in before):
            _append_log(email, f"[换绑] 尾号 {card['last4']} 已在列表，跳过添加")
        else:
            open_add_card_form(page)
            fill_add_card_form(page, card={
                "number": card["number"], "exp_month": card["exp_month"],
                "exp_year": card["exp_year"], "cvc": card["cvv"],
            }, billing=billing)
            click_add_submit(page)
            now = wait_card_list_change(page, before, want_last4=card["last4"], max_rounds=10)
            if not any(c["last4"] == card["last4"] for c in now):
                # 迟迟不出现且存在挑战帧 → 自动打码重提交（最多 2 轮）
                solver = _make_captcha_solver(ua)
                for _attempt in range(2):
                    if not (has_hcaptcha_challenge(page) and solver):
                        break
                    _append_log(email, "[换绑] 加卡疑似被 hCaptcha 阻拦，2Captcha 解码重提交")
                    if not solve_and_resubmit(page, email=email, solve_captcha=solver):
                        break
                    now = wait_card_list_change(page, before, want_last4=card["last4"], max_rounds=8)
                    if any(c["last4"] == card["last4"] for c in now):
                        break
                if not any(c["last4"] == card["last4"] for c in now):
                    try:
                        page.screenshot(path=str(log_path(email).with_suffix(".png")), full_page=True)
                    except Exception:  # noqa: BLE001
                        pass
                    raise RuntimeError(
                        f"新卡 {card['last4']} 提交后未出现在支付方式列表（可能被拒或需人工，截图见日志目录）")
            _append_log(email, f"[换绑] 新卡 {card['last4']} 已添加")

        # 6) 新卡设默认
        if not any(c["last4"] == card["last4"] and c["default"] for c in read_payment_methods(page)):
            set_default_card(page, card["last4"], email=email)
            _append_log(email, f"[换绑] 已把 {card['last4']} 设为默认")
        # 设默认会触发整页刷新+toast，等稳定再操作旧卡菜单
        page.wait_for_timeout(5000)

        # 7) 删旧卡
        for old in read_payment_methods(page):
            if old["last4"] == card["last4"]:
                continue
            remove_card(page, old["last4"], email=email)
            _append_log(email, f"[换绑] 已删除旧卡 {old['last4']}")

        # 8) 回读校验（删除后页面会重渲染+toast，轮询到列表只含新卡为止）
        final_cards = []
        for _ in range(8):
            page.wait_for_timeout(5000)
            final_cards = read_payment_methods(page)
            if final_cards and all(c["last4"] == card["last4"] for c in final_cards):
                break
        ok_new = any(c["last4"] == card["last4"] for c in final_cards)
        ok_default = any(c["last4"] == card["last4"] and c["default"] for c in final_cards)
        ok_old_gone = not any(c["last4"] != card["last4"] for c in final_cards)
        _append_log(email, f"[换绑] 最终卡列表：{json.dumps(final_cards, ensure_ascii=False)}")
        if not (ok_new and ok_default and ok_old_gone):
            raise RuntimeError(
                f"回读校验未通过：新卡在={ok_new} 默认={ok_default} 旧卡清空={ok_old_gone}")

        db.update_account_card_rebind(account_id, {
            "ok": True, "status": "success", "card_label": card["label"],
            "message": f"已换绑到 {card['label']}（默认，旧卡已删）",
            "checked_at": datetime.now().isoformat(timespec="seconds")})
        _append_log(email, f"[换绑] 完成：{card['label']}")
        return {"ok": True, "status": "success", "card_label": card["label"]}
    except Exception as exc:
        result = {"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        try:
            db.update_account_card_rebind(account_id, result)
        except Exception:
            logger.exception("[换绑] 写回失败状态失败: account_id=%s", account_id)
        try:
            _append_log(email, f"[换绑] 失败：{result['error']}")
        except Exception:
            pass
        logger.exception("[换绑] 后台异常: %s", email)
        return result
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        if fh is not None:
            try:
                root_logger.removeHandler(fh)
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        with _LOCK:
            _RUNNING.discard(int(account_id))
            _OUTSTANDING = max(0, _OUTSTANDING - 1)
            _LOCK.notify_all()


def queue_settings() -> dict:
    apply_settings()
    with _LOCK:
        return {
            "workers": _WORKERS,
            "queue_limit": _QUEUE_LIMIT,
            "running": len(_RUNNING),
            "outstanding": _OUTSTANDING,
        }


def enqueue_account_card_rebind(
    *, account_id: int, email: str, card_id: str = "",
    trigger: str = "manual", proxy: str | None = None,
) -> dict:
    global _OUTSTANDING
    apply_settings()
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    with _LOCK:
        if _OUTSTANDING >= _QUEUE_LIMIT:
            return {"accepted": False, "busy": False, "queue_full": True, "error": "换绑队列已满，请稍后重试"}
        _OUTSTANDING += 1
    if not db.claim_account_card_rebind(acc_id=account_id, trigger=trigger):
        _release_outstanding()
        return {"accepted": False, "busy": True, "error": "该账号正在换绑"}

    _append_log(email, f"[换绑] 已入队 account_id={account_id} card={card_id or '黑名单默认'} trigger={trigger}", clear=True)
    try:
        future = _EXECUTOR.submit(
            _run_card_rebind,
            account_id=account_id, email=email, card_id=str(card_id or ""),
            proxy=proxy, trigger=str(trigger or "manual"),
        )
        return {"accepted": True, "busy": False, "future": future, "log_path": str(log_path(email))}
    except Exception as exc:
        _release_outstanding()
        db.update_account_card_rebind(
            account_id, {"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return {"accepted": False, "busy": False, "error": f"{type(exc).__name__}: {exc}"}
