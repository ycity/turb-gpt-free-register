# -*- coding: utf-8 -*-
"""席位账号导入后预热调度。

导入席位账号后自动"登录拿 Token → 测席位 → 单席位自动换绑 2FA"。
底层复用查活队列（live_check_service.enqueue_account_live_check）：查活
成功即刷新 AT，随后在组线程内调用「测席位」探测（core.single_seat
.probe_single_seat，结果落 single_seat_* 字段，与账号页「测席位」按钮
同源）；探测确认单席位（supported=True）后，再调用「换绑2FA」
（core.totp_rotate_service.rotate_totp_secret，把卖家密钥换成本地新密钥，
旧密钥自动备份、fresh AT 一并写回）。查活自身仍会在同会话顺带查套餐。

并发约束：同一个注册代理（proxy_used）的账号必须串行执行（查活、测席位、
换绑都在组线程内完成），避免同一出口 IP 同时多次登录触发风控；不同代理
分组并行，组数受 _GROUP_WORKERS 限制。调度状态仅存内存，重启后由
recover_interrupted_seat_warmups() 把"已导入但从未执行过查活"的账号
重新排入队列。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from core import db
from core.chatgpt_plan import _mask_proxy

logger = logging.getLogger(__name__)

# 同时推进的代理分组数上限；单账号查活仍受查活队列全局 worker 数约束。
_GROUP_WORKERS = 4
_EXECUTOR = ThreadPoolExecutor(max_workers=_GROUP_WORKERS, thread_name_prefix="seat-warmup")
_LOCK = threading.Lock()
_ACTIVE: set[int] = set()
# proxy_key -> {"proxy","total","done","failed","pending","current","finished",...}
_GROUPS: dict[str, dict] = {}


def _group_key(proxy_used) -> str:
    return str(proxy_used or "").strip()


def _group_snapshot(info: dict) -> dict:
    with _LOCK:
        return dict(info)


def is_warming(account_id: int) -> bool:
    with _LOCK:
        return int(account_id) in _ACTIVE


def warmup_status() -> dict:
    with _LOCK:
        groups = [dict(value) for value in _GROUPS.values()]
        active = len(_ACTIVE)
    accounts_total = sum(int(g.get("total") or 0) for g in groups)
    done_total = sum(int(g.get("done") or 0) for g in groups)
    failed_total = sum(int(g.get("failed") or 0) for g in groups)
    probe_failed_total = sum(int(g.get("probe_failed") or 0) for g in groups)
    rotate_done_total = sum(int(g.get("rotate_done") or 0) for g in groups)
    rotate_failed_total = sum(int(g.get("rotate_failed") or 0) for g in groups)
    return {
        "active_accounts": active,
        "groups_total": len(groups),
        "accounts_total": accounts_total,
        "done_total": done_total,
        "failed_total": failed_total,
        "probe_failed_total": probe_failed_total,
        "rotate_done_total": rotate_done_total,
        "rotate_failed_total": rotate_failed_total,
        "group_workers": _GROUP_WORKERS,
        "groups": groups,
    }


def enqueue_seat_warmup(accounts: list[dict]) -> dict:
    """
    把席位账号按注册代理分组排入预热；同组串行、不同组并行。

    accounts 元素：{"id", "email", "proxy_used"}（db.import_seat_accounts
    返回的 inserted_rows 即可直接传入）。已在预热队列中的账号会跳过。
    """
    groups: dict[str, list[dict]] = {}
    accepted: list[int] = []
    skipped: list[dict] = []
    for acc in accounts or []:
        try:
            account_id = int(acc.get("id"))
        except (TypeError, ValueError):
            continue
        email = str(acc.get("email") or "").strip()
        if not email:
            skipped.append({"id": acc.get("id"), "reason": "邮箱为空"})
            continue
        with _LOCK:
            if account_id in _ACTIVE:
                skipped.append({"id": account_id, "email": email, "reason": "已在预热队列"})
                continue
            _ACTIVE.add(account_id)
        groups.setdefault(_group_key(acc.get("proxy_used")), []).append(acc)
        accepted.append(account_id)

    # 顺手清理上一轮已完成的分组快照，避免状态无限堆积。
    with _LOCK:
        for key in [key for key, value in _GROUPS.items() if value.get("finished")]:
            _GROUPS.pop(key, None)

    for proxy, accs in groups.items():
        _EXECUTOR.submit(_run_group, proxy, accs)
    return {
        "queued": len(accepted),
        "groups": len(groups),
        "skipped": skipped,
        "group_workers": _GROUP_WORKERS,
    }


def _probe_single_seat(account_id: int, email: str) -> dict:
    """查活拿到新 AT 后测席位（复用「测席位」按钮的探测逻辑与落库）。"""
    from core.single_seat import probe_single_seat

    acc = db.get_account(account_id) or {}
    res = probe_single_seat(acc)
    db.update_account_single_seat(account_id, res)
    return res


def _probe_ok(probe: dict) -> bool:
    """测席位成功且命中单席位、首月免费或任意折扣（任一成立即值得自动换绑 2FA）。"""
    has_discount = (
        float(probe.get("checkout_discount_pct") or 0) > 0
        or int(probe.get("checkout_discount_minor") or 0) > 0
    ) if isinstance(probe, dict) else False
    return (
        bool(probe)
        and probe.get("ok") is True
        and (probe.get("supported") is True or probe.get("free_trial") is True or has_discount)
    )


def _rotate_2fa(account_id: int, email: str) -> dict:
    """单席位账号的 2FA 换绑：把卖家密钥换成本地新生成的密钥。

    复用「换绑2FA」按钮的 rotate_totp_secret：新鲜登录 → enroll 新 TOTP →
    激活 → 落库（旧密钥自动备份到 cache/totp_backups，fresh AT 一并写回）。
    """
    from core.totp_rotate_service import rotate_totp_secret

    return rotate_totp_secret(account_id, trigger="seat_import")


def _run_group(proxy: str, accounts: list[dict]) -> None:
    """单个代理分组的串行执行器：上一个账号查活结束才入队下一个。"""
    # 延迟导入：查活服务持有线程池，避免模块加载顺序问题。
    from core import live_check_service

    masked = _mask_proxy(proxy) or "直连/未配置代理"
    total = len(accounts)
    with _LOCK:
        info = _GROUPS.setdefault(proxy, {
            "proxy": masked, "total": total, "done": 0, "failed": 0,
            "probe_failed": 0, "rotate_done": 0, "rotate_failed": 0,
            "pending": total, "current": None,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    logger.info("[席位预热] 分组开始 proxy=%s 账号数=%d（同组串行）", masked, total)
    for acc in accounts:
        account_id = int(acc["id"])
        email = str(acc.get("email") or "")
        try:
            with _LOCK:
                info["current"] = email
                info["pending"] = max(0, int(info.get("pending") or 0) - 1)
            queued = live_check_service.enqueue_account_live_check(
                account_id=account_id,
                email=email,
                trigger="seat_import",
                # 使用导入时分配的注册代理；查活内部网络失败仍会按既有
                # 策略换池内其他代理重试，这里只负责同代理串行。
                proxy=proxy or None,
                return_future=True,
            )
            if not queued.get("accepted"):
                with _LOCK:
                    info["failed"] = int(info.get("failed") or 0) + 1
                    info["done"] = int(info.get("done") or 0) + 1
                logger.warning("[席位预热] %s 未能入队查活：%s", email, queued.get("error"))
                continue
            result = {}
            future = queued.get("future")
            if future is not None:
                result = future.result() or {}
            ok = bool(result.get("ok"))
            with _LOCK:
                if not ok:
                    info["failed"] = int(info.get("failed") or 0) + 1
                info["done"] = int(info.get("done") or 0) + 1
            logger.info(
                "[席位预热] %s 完成 ok=%s status=%s",
                email, ok, result.get("status"),
            )
            # 查活成功即已刷新 AT；在组线程内继续测席位（仍占用同一代理，
            # 不破坏同代理串行约束）。探测失败不影响查活结果。
            probe: dict = {}
            if ok:
                try:
                    probe = _probe_single_seat(account_id, email)
                    if probe.get("ok"):
                        logger.info(
                            "[席位预热] %s 测席位：supported=%s minimum=%s free_trial=%s discount_pct=%s price=%s %s 出口=%s/%s",
                            email, probe.get("supported"), probe.get("minimum_seats"),
                            probe.get("free_trial"), probe.get("checkout_discount_pct"),
                            probe.get("checkout_total_amount"),
                            probe.get("checkout_currency") or probe.get("currency"),
                            probe.get("country"), probe.get("currency"),
                        )
                    else:
                        with _LOCK:
                            info["probe_failed"] = int(info.get("probe_failed") or 0) + 1
                        logger.warning(
                            "[席位预热] %s 测席位失败：%s", email, probe.get("error"))
                except Exception as exc:
                    with _LOCK:
                        info["probe_failed"] = int(info.get("probe_failed") or 0) + 1
                    logger.exception("[席位预热] %s 测席位异常：%s", email, exc)
                # 单席位账号：测出后立即执行 2FA 换绑（卖家密钥 → 本地新密钥）。
                # 仍在组线程内同步执行，保持同代理串行。
                if _probe_ok(probe):
                    try:
                        rr = _rotate_2fa(account_id, email)
                        if rr.get("ok"):
                            with _LOCK:
                                info["rotate_done"] = int(info.get("rotate_done") or 0) + 1
                            logger.info(
                                "[席位预热] %s 单席位，已自动换绑 2FA（新密钥 %s...%s）",
                                email, str(rr.get("new_secret"))[:4], str(rr.get("new_secret"))[-4:])
                        else:
                            with _LOCK:
                                info["rotate_failed"] = int(info.get("rotate_failed") or 0) + 1
                            logger.warning(
                                "[席位预热] %s 单席位 2FA 换绑失败：%s", email, rr.get("error"))
                    except Exception as exc:
                        with _LOCK:
                            info["rotate_failed"] = int(info.get("rotate_failed") or 0) + 1
                        logger.exception("[席位预热] %s 单席位 2FA 换绑异常：%s", email, exc)
        except Exception as exc:
            with _LOCK:
                info["failed"] = int(info.get("failed") or 0) + 1
                info["done"] = int(info.get("done") or 0) + 1
            logger.exception("[席位预热] %s 预热异常：%s", email, exc)
        finally:
            with _LOCK:
                _ACTIVE.discard(account_id)
                info["current"] = None
    with _LOCK:
        info["finished"] = True
        info["current"] = None
    logger.info(
        "[席位预热] 分组完成 proxy=%s 总数=%d 查活失败=%d 测席位失败=%d",
        masked, total, int(info.get("failed") or 0), int(info.get("probe_failed") or 0))


def recover_interrupted_seat_warmups() -> int:
    """
    服务重启后，把"已导入但从未执行过查活"的席位账号重新排入预热。

    只补导入后从未执行的账号（live_check_status 为空）。查活已跑过的
    （包括真实失败，如密码错误）不重复重试，避免每次重启都打一遍必败登录。
    """
    try:
        pending = db.list_seat_accounts_pending_warmup()
    except Exception:
        logger.exception("[席位预热] 启动恢复查询失败")
        return 0
    if not pending:
        return 0
    out = enqueue_seat_warmup(pending)
    if out.get("queued"):
        logger.info(
            "[席位预热] 启动恢复：重新排队 %d 个席位账号（%d 组代理，同组串行）",
            out.get("queued"), out.get("groups"),
        )
    return int(out.get("queued") or 0)
