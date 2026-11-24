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
# 连续失败次数。失败后用短间隔快速换出口+换邮箱重试，
# 但连续失败太多次说明是系统性故障（出口全废、邮箱池全坏），
# 这时回落到正常间隔，免得以 2 分钟一次的频率持续烧号。
_FAIL_STREAK_KEY = "auto_register_fail_streak"
# 上一个由自动注册发起的任务 id。下一轮据它结算成功/失败，从而决定
# 本轮用正常间隔还是失败快速重试。
_LAST_JOB_KEY = "auto_register_last_job"

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


def _retry_interval() -> float:
    """注册失败后的短重试间隔。不低于 30s，免得失败时疯狂空转。"""
    try:
        return max(30.0, float(getattr(_cfg, "AUTO_REGISTER_RETRY_INTERVAL", 120.0)))
    except (TypeError, ValueError):
        return 120.0


def _max_fast_retries() -> int:
    try:
        return max(0, int(getattr(_cfg, "AUTO_REGISTER_MAX_FAST_RETRIES", 3)))
    except (TypeError, ValueError):
        return 3


def _fail_streak() -> int:
    try:
        return max(0, int(str(db.get_meta(_FAIL_STREAK_KEY) or "0")))
    except (TypeError, ValueError):
        return 0


def _set_fail_streak(n: int) -> None:
    db.set_meta(_FAIL_STREAK_KEY, str(max(0, int(n))))


def _effective_interval() -> tuple[float, bool]:
    """返回 (本轮该用的间隔, 是否处于快速重试).

    上一轮失败、且连续失败还没到上限时用短间隔——失败多半是出口被挡、
    邮箱已废这类换一组就能解决的问题，没必要等满正常间隔。
    连续失败到上限说明是系统性故障，回落到正常间隔等人介入。
    """
    streak = _fail_streak()
    if 0 < streak <= _max_fast_retries():
        return _retry_interval(), True
    return interval_seconds(), False


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
    interval, fast = _effective_interval()
    if elapsed < interval:
        mode = "失败快速重试" if fast else "正常间隔"
        return {"blocked": True,
                "reason": f"距上轮 {int(elapsed)}s，未满 {int(interval)}s（{mode}）",
                "state": "waiting", "available": remaining,
                "wait_seconds": int(interval - elapsed),
                "fast_retry": fast, "fail_streak": _fail_streak(),
                "anchor": anchor.isoformat(timespec="seconds")}

    tail = "，失败后快速重试" if fast else ""
    return {"blocked": False, "reason": f"距上轮已 {int(elapsed)}s，发起下一个{tail}",
            "state": "ready", "available": remaining,
            "fast_retry": fast, "fail_streak": _fail_streak(),
            "anchor": anchor.isoformat(timespec="seconds")}


def _settle_last_job() -> None:
    """结算上一个自动发起的任务，更新连续失败计数。

    成功 -> 清零；失败 -> +1（触发下一轮快速重试）。
    任务还没结束就先不动，等它跑完下一轮再结算。
    成功路径其实在 note_twofa_finished 已经清零了，这里主要兜失败那条：
    注册失败时 2FA 不会入队，没有别的地方会把 streak 推上去。
    """
    raw = str(db.get_meta(_LAST_JOB_KEY) or "").strip()
    if not raw:
        return
    try:
        job = db.get_job(int(raw))
    except Exception:  # noqa: BLE001 - 查不到就当它已消失，别卡住调度
        job = None
    if not job:
        db.set_meta(_LAST_JOB_KEY, "")
        return
    st = str(job.get("status") or "").strip().lower()
    if st in ("success", "succeeded", "done", "completed"):
        if _fail_streak():
            logger.info("[自动注册] 上一个任务 #%s 成功，清零连续失败计数", raw)
        _set_fail_streak(0)
        db.set_meta(_LAST_JOB_KEY, "")
    elif st in ("failed", "error", "cancelled", "canceled"):
        streak = _fail_streak() + 1
        _set_fail_streak(streak)
        db.set_meta(_LAST_JOB_KEY, "")
        logger.warning("[自动注册] 上一个任务 #%s 失败（%s），连续失败 %s 次，"
                       "将换出口+换邮箱按 %ss 间隔快速重试",
                       raw, st, streak, int(_retry_interval()))
    # 其它状态（queued/running）：还没结束，保留 job_id 下轮再看


def _tick() -> dict:
    """执行一次判定；满足条件就发起一个注册任务。"""
    # 先结算上一个任务，连续失败计数要在判定间隔之前更新
    _settle_last_job()

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
    # 记下这个任务，下一轮据它成功/失败决定走正常间隔还是快速重试
    db.set_meta(_LAST_JOB_KEY, str(job_id or ""))
    logger.info("[自动注册] 已发起注册任务 #%s", job_id)
    return {"blocked": False, "state": "submitted", "job_id": job_id,
            "reason": f"已发起注册任务 #{job_id}",
            "submitted_at": started_at.isoformat(timespec="seconds")}


def note_twofa_finished(email: str | None = None) -> None:
    """2FA 收尾时把锚点推到当前时刻，作为下一轮间隔的起点。"""
    moment = _now()
    _record_anchor(moment)
    # 走到 2FA 说明注册这步成功了，清零连续失败计数，回到正常间隔
    if _fail_streak():
        logger.info("[自动注册] 2FA 完成，清零连续失败计数")
    _set_fail_streak(0)
    db.set_meta(_LAST_JOB_KEY, "")
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
        "fast_retry": bool(decision.get("fast_retry")),
        "fail_streak": _fail_streak(),
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
