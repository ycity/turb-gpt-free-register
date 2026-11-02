# -*- coding: utf-8 -*-
"""
自动注册调度：邮箱池有号时按固定间隔发起注册，每次只跑一个。

计时锚点是上一个号 2FA 结束的时刻，不是注册任务结束的时刻。当前注册驱动
（cloak/roxy）都不在注册流程里内联设置 2FA，而是账号落库后入队
core.twofa_service 异步执行，还带 TWOFA_REGISTRATION_DELAY 冷却。按任务完成
时间计时会在 2FA 还没跑完时就发起下一个。
"""
import logging
import threading
from datetime import datetime

from config import auto_register as _cfg
from core import db, registration_service as svc

logger = logging.getLogger(__name__)

_ANCHOR_KEY = "auto_register_last_2fa_at"
_ENABLED_KEY = "auto_register_enabled"

_THREAD: threading.Thread | None = None
_WAKE = threading.Event()
_LOCK = threading.Lock()
_LAST_DECISION: dict = {"reason": "尚未开始检查", "blocked": True}


def _now() -> datetime:
    return datetime.now()


def _parse(stamp: object) -> datetime | None:
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def is_enabled() -> bool:
    """开关以 storage_meta 为准，未设置过则回落到配置默认值。"""
    stored = db.get_meta(_ENABLED_KEY)
    if stored is None:
        return bool(getattr(_cfg, "AUTO_REGISTER_ENABLED", False))
    return stored == "1"


def set_enabled(value: bool) -> bool:
    db.set_meta(_ENABLED_KEY, "1" if value else "0")
    _WAKE.set()
    logger.info("[自动注册] 开关已%s", "开启" if value else "关闭")
    return bool(value)


def interval_seconds() -> float:
    try:
        return max(60.0, float(getattr(_cfg, "AUTO_REGISTER_INTERVAL", 1800.0)))
    except (TypeError, ValueError):
        return 1800.0


def _twofa_timeout() -> float:
    try:
        return max(60.0, float(getattr(_cfg, "AUTO_REGISTER_2FA_TIMEOUT", 1800.0)))
    except (TypeError, ValueError):
        return 1800.0


def _poll_interval() -> float:
    try:
        return max(5.0, float(getattr(_cfg, "AUTO_REGISTER_POLL_INTERVAL", 30.0)))
    except (TypeError, ValueError):
        return 30.0


def available_emails() -> int:
    """可发起注册的本地邮箱余量。

    临时邮箱来源（gptmail/mailnest 等）按需生成，没有可枯竭的本地池，
    视为永远有号；只有本地池来源才计数。
    """
    from core.email_provider import parse_email_sources
    from config import email as _email_cfg

    on_demand = {"gptmail", "mailnest", "cloudmail", "remail", "xigua", "mailcode", "cloudflare"}
    total = 0
    for source in parse_email_sources(getattr(_email_cfg, "EMAIL_SOURCE", "outlook")):
        if source in on_demand:
            return -1
        if source == "generic_api":
            total += int(db.generic_api_email_pool_summary().get("available", 0) or 0)
        elif source == "imap":
            total += int(db.imap_email_pool_summary().get("available", 0) or 0)
        elif source == "cloudflare_domain":
            total += int(db.domain_email_pool_summary().get("available", 0) or 0)
        else:
            total += int(db.outlook_pool_summary().get("available", 0) or 0)
    return total


def _anchor() -> datetime | None:
    """上一轮 2FA 结束时刻：优先本调度器自己的记录，否则取账号表最新一条。"""
    stored = _parse(db.get_meta(_ANCHOR_KEY))
    if stored is not None:
        return stored
    return _parse(db.totp_setup_activity().get("last_completed_at"))


def _record_anchor(moment: datetime) -> None:
    db.set_meta(_ANCHOR_KEY, moment.isoformat(timespec="seconds"))


def evaluate() -> dict:
    """判断此刻是否该发起注册，返回判定详情（不产生副作用）。"""
    now = _now()
    if not is_enabled():
        return {"blocked": True, "reason": "自动注册未开启", "state": "disabled"}

    remaining = available_emails()
    if remaining == 0:
        return {"blocked": True, "reason": "邮箱池已空，停止自动注册",
                "state": "pool_empty", "available": remaining}

    counts = db.job_status_counts()
    active = int(counts.get("active", 0) or 0)
    if active > 0:
        return {"blocked": True, "reason": f"有 {active} 个注册任务未结束，保持单线程",
                "state": "job_active", "available": remaining}

    twofa = db.totp_setup_activity()
    pending = int(twofa.get("pending", 0) or 0)
    if pending > 0:
        since = _parse(twofa.get("pending_since"))
        waited = (now - since).total_seconds() if since else 0.0
        if since is None or waited < _twofa_timeout():
            return {"blocked": True, "reason": f"上一个号的 2FA 仍在进行（已等 {int(waited)}s）",
                    "state": "twofa_pending", "available": remaining}
        logger.warning("[自动注册] 2FA 已等待 %.0fs 超过上限，不再等待", waited)

    anchor = _anchor()
    if anchor is None:
        return {"blocked": False, "reason": "无历史记录，立即发起首个任务",
                "state": "ready", "available": remaining}

    elapsed = (now - anchor).total_seconds()
    interval = interval_seconds()
    if elapsed < interval:
        return {"blocked": True, "reason": f"距上次 2FA 结束 {int(elapsed)}s，未满 {int(interval)}s",
                "state": "waiting", "available": remaining,
                "wait_seconds": int(interval - elapsed),
                "anchor": anchor.isoformat(timespec="seconds")}

    return {"blocked": False, "reason": f"距上次 2FA 结束已 {int(elapsed)}s，发起下一个",
            "state": "ready", "available": remaining,
            "anchor": anchor.isoformat(timespec="seconds")}


def _tick() -> dict:
    """执行一次判定；满足条件就发起一个注册任务。"""
    decision = evaluate()
    if decision.get("blocked"):
        return decision

    # 发起前先占住锚点：若注册任务很快失败且没有产生账号，2FA 不会入队，
    # 锚点就是唯一的节流依据，否则会陷入每轮都立刻重试的循环。
    started_at = _now()
    _record_anchor(started_at)
    try:
        jobs = svc.submit_registration(count=1, workers=1)
    except Exception as exc:
        logger.exception("[自动注册] 发起注册失败")
        return {"blocked": True, "state": "submit_failed",
                "reason": f"发起注册失败：{type(exc).__name__}: {exc}"}

    job_id = int(jobs[0]["id"]) if jobs else None
    logger.info("[自动注册] 已发起注册任务 #%s", job_id)
    return {"blocked": False, "state": "submitted", "job_id": job_id,
            "reason": f"已发起注册任务 #{job_id}",
            "submitted_at": started_at.isoformat(timespec="seconds")}


def note_twofa_finished(email: str | None = None) -> None:
    """2FA 收尾时把锚点推到当前时刻，作为下一轮间隔的起点。"""
    moment = _now()
    _record_anchor(moment)
    logger.info("[自动注册] 2FA 已结束（%s），下一轮从 %s 开始计时",
                email or "未知邮箱", moment.strftime("%H:%M:%S"))
    _WAKE.set()


def status() -> dict:
    """给 WebUI 的状态快照。"""
    decision = evaluate()
    anchor = _anchor()
    with _LOCK:
        last = dict(_LAST_DECISION)
    remaining = decision.get("available")
    if remaining is None:
        remaining = available_emails()
    return {
        "enabled": is_enabled(),
        "interval": int(interval_seconds()),
        "poll_interval": int(_poll_interval()),
        "available_emails": remaining,
        "unlimited_pool": remaining == -1,
        "last_2fa_at": anchor.isoformat(timespec="seconds") if anchor else None,
        "next_eta_seconds": int(decision.get("wait_seconds") or 0),
        "state": decision.get("state"),
        "reason": decision.get("reason"),
        "blocked": bool(decision.get("blocked")),
        "running": _THREAD is not None and _THREAD.is_alive(),
        "last_action": last,
    }


def _loop() -> None:
    logger.info("[自动注册] 调度线程已启动")
    while True:
        _WAKE.wait(timeout=_poll_interval())
        _WAKE.clear()
        try:
            decision = _tick()
        except Exception as exc:
            decision = {"blocked": True, "state": "error",
                        "reason": f"{type(exc).__name__}: {exc}"}
            logger.exception("[自动注册] 调度循环异常")
        decision["checked_at"] = _now().isoformat(timespec="seconds")
        with _LOCK:
            global _LAST_DECISION
            _LAST_DECISION = decision


def start() -> None:
    """启动调度线程；重复调用无副作用。"""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_loop, name="auto-register", daemon=True)
        _THREAD.start()
