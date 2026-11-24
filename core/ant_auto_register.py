# -*- coding: utf-8 -*-
"""Ant Browser 注册自动化：按需批量注册，逐个走「注册 → 冷却 → 自动 2FA → 校验」。

与手点 WebUI「开始注册」是同一条路径——通过 WebUI 任务接口提交
（POST /api/jobs，count=1，workers=1），任务在主进程里执行；本模块只做
预检、节奏控制、等待和验收，不自己跑注册流程。

为什么要串行并等 2FA：HK 节点的统一 CDP 入口同一时刻只有一个活动窗口；
2FA 绑定也走节点，且带注册后冷却（默认 180s）。上一个号的 2FA 没落地就
发起下一个，只会排在节点锁上干等，也没法第一时间发现 2FA 失败。

节奏：
- 两个号之间的间隔默认取 AUTO_REGISTER_INTERVAL（与自动注册调度器同一个值，
  从上一个号 2FA 结束算起），--gap 可覆盖（0 = 不额外等待）。
- 连续失败达到 --max-fail（默认 2）就停：每次失败都可能消耗一个池邮箱
  （半注册/已停用），连败通常意味着出口或邮箱池有系统性问题，该停下来看。
- 中断本脚本（Ctrl-C）不会打断已提交的注册任务——任务在主进程里，会自己跑完。

接管半注册邮箱（takeover 子命令）：邮箱池里标 failed 的半注册邮箱——OpenAI 侧
已建号、密码也提交了，只是创建账户的验证码/资料页没走完。从登录页用已有密码
（没有密码的用一次性验证码）登录，补完验证和资料后入库，再等自动 2FA。
这一步没有对应的 WebUI 任务类型，所以在本进程里直接跑页面流程。

用法（容器内，时间均为 UTC）:
    python core/ant_auto_register.py run --count 3 [--max-fail 2] [--gap 0]
    python core/ant_auto_register.py preflight
    python core/ant_auto_register.py takeover --list
    python core/ant_auto_register.py takeover --all
    python core/ant_auto_register.py takeover --email user@example.com [--password ...]
宿主机入口:
    scripts/ant_auto_register.sh [数量] [最大连续失败] [--gap 秒]
    scripts/ant_auto_register.sh --takeover [--list | --all | --email user@example.com]

每个号结束输出一行 `RESULT {...}`（JSON），便于外部脚本解析。
退出码: 0 全部成功；1 有失败或提前停止；2 预检未通过（一个号都没处理）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_TERMINAL = ("success", "failed", "stopped", "cancelled")
_2FA_ACTIVE = ("queued", "running")
# 这些预检项不满足时等一等就能恢复（别的任务/2FA/别的实例占着节点）
_WAITABLE = {"没有未结束的注册任务", "没有进行中的 2FA", "HK 节点空闲"}

EXIT_OK, EXIT_PARTIAL, EXIT_PREFLIGHT = 0, 1, 2


def _log(message: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message), flush=True)


# ---------------------------------------------------------------- WebUI 接口

def _webui_base() -> str:
    # 容器内 WebUI 监听 5000（宿主 5002 是端口映射）
    return os.environ.get("WEBUI_INTERNAL_BASE", "http://127.0.0.1:5000").rstrip("/")


def _webui_auth_code() -> str:
    value = str(os.environ.get("WEBUI_AUTH_CODE", "") or "").strip()
    if value:
        return value
    try:
        with open(os.path.join(_PROJECT_ROOT, ".env"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("WEBUI_AUTH_CODE="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _webui(method: str, path: str, body: dict | None = None, timeout: float = 20.0):
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_webui_base() + path, data=data, method=method)
    code = _webui_auth_code()
    if code:
        req.add_header("X-Auth-Code", code)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8") or "null")


def submit_one() -> int:
    """提交 1 个注册任务到主进程，返回任务 id。workers 固定为 1（节点串行）。"""
    status, data = _webui("POST", "/api/jobs", {"count": 1, "workers": 1})
    payload = data if isinstance(data, dict) else {}
    if status != 200 or not payload.get("ok"):
        raise RuntimeError("HTTP %s %s" % (status, str(data)[:200]))
    jobs = payload.get("jobs") or []
    if not jobs or not jobs[0].get("id"):
        raise RuntimeError("接口未返回任务：%s" % str(data)[:200])
    return int(jobs[0]["id"])


# ------------------------------------------------------------------ 状态读取

def _idle_blocker() -> str | None:
    """节点/队列不空闲时返回原因，空闲返回 None。"""
    from core import db

    active = int((db.job_status_counts() or {}).get("active") or 0)
    if active:
        return "%s 个注册任务未结束" % active
    pending = int((db.totp_setup_activity() or {}).get("pending") or 0)
    if pending:
        return "%s 个 2FA 进行中" % pending
    try:
        from core.ant_browser_client import AntBrowserClient
        info = AntBrowserClient().request("GET", "/api/runtime/active", timeout=8)
        if (info or {}).get("active"):
            return "节点上有其它实例在运行"
    except Exception as exc:  # noqa: BLE001 - 查不到状态不能当成空闲
        return "节点状态查询失败：%s" % str(exc)[:80]
    return None


def wait_until_idle(timeout: float, poll: float = 15.0) -> bool:
    deadline = time.time() + timeout
    last = ""
    while True:
        blocker = _idle_blocker()
        if blocker is None:
            return True
        if time.time() >= deadline:
            _log("等待空闲超时（%ss）：%s" % (int(timeout), blocker))
            return False
        if blocker != last:
            _log("等待中：%s" % blocker)
            last = blocker
        time.sleep(poll)


def wait_job(job_id: int, timeout: float, poll: float = 5.0) -> dict:
    """等任务进入终态；超时则返回当时的任务快照（状态仍非终态）。"""
    from core import db

    deadline = time.time() + timeout
    last = ""
    while True:
        job = db.get_job(job_id) or {}
        status = str(job.get("status") or "")
        if status in _TERMINAL or time.time() >= deadline:
            return job
        if status != last:
            _log("任务 #%s：%s（%s）" % (job_id, status, job.get("email") or "未分配邮箱"))
            last = status
        time.sleep(poll)


def wait_twofa(account_id: int, queue_wait: float, timeout: float) -> dict:
    """等账号的自动 2FA 落地。

    返回 state: success / failed / stopped / not_queued / timeout。
    """
    from core import db

    deadline = time.time() + queue_wait
    while time.time() < deadline:
        status = str((db.get_account(account_id) or {}).get("totp_setup_status") or "").lower()
        if status:
            break  # 已入队（或已经跑完）
        time.sleep(5)
    else:
        return {"state": "not_queued"}

    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        acc = db.get_account(account_id) or {}
        status = str(acc.get("totp_setup_status") or "").lower()
        if status not in _2FA_ACTIVE:
            if status == "success" and acc.get("totp_secret"):
                return {"state": "success"}
            return {"state": status or "failed",
                    "error": str(acc.get("totp_setup_error") or "")[:200]}
        if status != last:
            _log("  2FA：%s" % status)
            last = status
        time.sleep(10)
    return {"state": "timeout"}


def _proxy_exit_from_log(job: dict) -> str:
    """从任务日志里取本单实际使用的代理出口（驱动在开跑时记录）。"""
    log_path = str(job.get("log_file") or "")
    if not log_path:
        return ""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r"本轮代理出口：(\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


def _classify_failure(error: object) -> tuple[str, str]:
    """按驱动的错误前缀判断失败类型和邮箱去向。"""
    text = str(error or "")
    if text.startswith("RegistrationAdvanced:"):
        return "half_registered", "OpenAI 侧已建号但流程未走完；邮箱标 failed，密码在池备注里待接管"
    if text.startswith("EmailAlreadyRegistered:"):
        return "already_registered", "邮箱在 OpenAI 侧已有账号；已停用"
    if text.startswith("EmailHasUnknown2FA:"):
        return "unknown_2fa", "账号带未知的 2FA 无法接管；邮箱已废弃"
    return "other", "邮箱已退回池子"


# -------------------------------------------------------------------- 预检

def collect_preflight(takeover: bool = False) -> list[dict]:
    """只读检查，返回 [{name, ok, detail}]。不产生任何副作用。

    takeover=True 用于接管半注册邮箱：不从池里领新邮箱、不提交注册任务，
    所以不查注册驱动、池余量和 WebUI 任务接口。
    """
    checks: list[dict] = []

    def add(name: str, ok: object, detail: object = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail or "")})

    from core import db
    if not takeover:
        from config import roxybrowser as _roxy_cfg
        driver = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "") or "").strip().lower()
        add("注册驱动为 ant", driver == "ant", "REGISTRATION_DRIVER=%s" % (driver or "<空>"))

    active = int((db.job_status_counts() or {}).get("active") or 0)
    add("没有未结束的注册任务", active == 0, "%s 个未结束" % active if active else "")
    twofa = db.totp_setup_activity() or {}
    pending = int(twofa.get("pending") or 0)
    add("没有进行中的 2FA", pending == 0,
        "%s 个进行中（自 %s）" % (pending, twofa.get("pending_since")) if pending else "")

    if not takeover:
        from core.auto_register_scheduler import available_emails
        remaining = available_emails()
        add("邮箱池有余量", remaining != 0,
            "可用 %s" % ("不限（按需生成）" if remaining == -1 else remaining))

    from core.ant_registration_bridge import ant_proxy_candidates
    from config.proxy import PROXY_POOL
    candidates = ant_proxy_candidates()
    add("有节点可达的代理出口", bool(candidates),
        "%s / %s 条可用" % (len(candidates), len(PROXY_POOL)))

    node_ok, node_idle, node_detail = False, False, ""
    try:
        from core.ant_browser_client import AntBrowserClient
        client = AntBrowserClient()
        node_ok = client.health()
        if node_ok:
            info = client.request("GET", "/api/runtime/active", timeout=8) or {}
            node_idle = not info.get("active")
            node_detail = "有实例在运行：%s" % info.get("profileName") if not node_idle else ""
        else:
            node_detail = "Launch API 无响应：%s" % client.api_base
    except Exception as exc:  # noqa: BLE001
        node_detail = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    add("HK 节点 Ant Browser 在线", node_ok, node_detail)
    add("HK 节点空闲", node_idle, node_detail if node_ok else "节点不在线，无从判断")

    if not takeover:
        try:
            status, _payload = _webui("GET", "/api/summary", timeout=10)
            add("WebUI 任务接口可用", status == 200, _webui_base())
        except Exception as exc:  # noqa: BLE001
            add("WebUI 任务接口可用", False, "%s: %s" % (type(exc).__name__, str(exc)[:120]))
    return checks


def print_preflight(checks: list[dict]) -> None:
    for c in checks:
        _log("  [%s] %s%s" % ("OK" if c["ok"] else "!!", c["name"],
                              (" —— " + c["detail"]) if c["detail"] else ""))
    try:
        from core.auto_register_scheduler import is_enabled
        if is_enabled():
            _log("  提示：自动注册调度器处于开启状态，可能与本脚本交替发起任务")
    except Exception:  # noqa: BLE001
        pass


# -------------------------------------------------------------------- 主流程

def _run_one(index: int, args: argparse.Namespace) -> dict:
    res: dict = {"index": index, "ok": False, "stage": "submit", "job_id": None,
                 "email": None, "account_id": None, "proxy_exit": "", "kind": "", "detail": ""}
    started = time.time()
    try:
        job_id = submit_one()
    except Exception as exc:  # noqa: BLE001
        res["detail"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        return res
    res["job_id"] = job_id
    _log("已提交任务 #%s，等待注册完成（最长 %s 分钟）" % (job_id, int(args.job_timeout // 60)))
    job = wait_job(job_id, args.job_timeout)
    res["email"] = job.get("email") or None
    res["proxy_exit"] = _proxy_exit_from_log(job)
    status = str(job.get("status") or "")
    res["seconds"] = int(time.time() - started)
    if status not in _TERMINAL:
        res.update(stage="register", kind="job_timeout",
                   detail="注册任务超时仍未结束；任务在主进程里继续跑，可在 WebUI 任务页查看")
        return res
    if status != "success":
        kind, where = _classify_failure(job.get("error_message"))
        res.update(stage="register", kind=kind,
                   detail="%s；%s；%s" % (where, kind, str(job.get("error_message") or "")[:160]))
        return res

    account_id = job.get("account_id")
    res["account_id"] = account_id
    _log("注册成功：%s → 账号 #%s（出口 %s，用时 %ss），等 2FA 落地" % (
        res["email"], account_id, res["proxy_exit"] or "?", res["seconds"]))
    res["stage"] = "twofa"
    if not account_id:
        res.update(kind="no_account_id", detail="任务成功但没有 account_id")
        return res
    return _wait_twofa_and_verify(res, args, started=started)


def _wait_twofa_and_verify(res: dict, args: argparse.Namespace, *, started: float,
                           require_password: bool = True) -> dict:
    """入库后等自动 2FA 落地，再校验密码 / TOTP 密钥 / accessToken 是否齐全。结果写回 res。"""
    res.setdefault("ok", False)
    res["stage"] = "twofa"
    tw = wait_twofa(int(res["account_id"]), args.twofa_queue_wait, args.twofa_timeout)
    state = tw.get("state")
    if state != "success":
        hint = ("检查 ENABLE_2FA 是否开启" if state == "not_queued"
                else "看 注册日志/twofa-<邮箱>.log")
        res.update(kind="twofa_" + str(state),
                   detail="2FA 未落地（%s）%s；%s" % (state, ("：" + tw["error"]) if tw.get("error") else "", hint))
        return res

    from core import db
    acc = db.get_account(int(res["account_id"])) or {}
    extra_raw = acc.get("extra_json")
    extra = json.loads(extra_raw) if isinstance(extra_raw, str) and extra_raw else (extra_raw or {})
    res["twofa_finished_at"] = time.time()
    has_password = bool(extra.get("registration_password"))
    missing = [name for name, present in (
        ("密码", has_password or not require_password),
        ("TOTP 密钥", bool(acc.get("totp_secret"))),
        ("accessToken", bool(acc.get("access_token"))),
    ) if not present]
    if missing:
        res.update(kind="incomplete", detail="账号入库但缺少：%s" % "、".join(missing))
        return res
    took = int(time.time() - started)
    if has_password:
        res.update(ok=True, stage="done", detail="密码 + 2FA 齐全，用时 %ss" % took)
    else:
        # 接管时没有密码记录：账号能登录（验证码 / TOTP），但还不是完整的池账号，要另行补密码
        res.update(ok=True, stage="done", kind="no_password",
                   detail="2FA 齐全，但没有密码记录（待补密码），用时 %ss" % took)
    return res


def run(args: argparse.Namespace) -> int:
    from config import auto_register as _auto_cfg
    from core.auto_register_scheduler import available_emails

    gap = args.gap
    if gap is None:
        gap = max(0.0, float(getattr(_auto_cfg, "AUTO_REGISTER_INTERVAL", 1800.0) or 0.0))
    _log("Ant 注册自动化：目标 %s 个；连败 %s 次即停；两号间隔 %s（从上一个号 2FA 结束算起）；时间均为 UTC"
         % (args.count, args.max_fail, ("%ss" % int(gap)) if gap > 0 else "不额外等待"))

    checks = collect_preflight()
    print_preflight(checks)
    hard = [c["name"] for c in checks if not c["ok"] and c["name"] not in _WAITABLE]
    if hard:
        _log("预检未通过，未提交任何任务：%s" % "、".join(hard))
        return EXIT_PREFLIGHT
    if not wait_until_idle(args.idle_wait):
        _log("节点/队列未空闲，未提交任何任务")
        return EXIT_PREFLIGHT

    results: list[dict] = []
    fail_streak = 0
    last_twofa_at: float | None = None
    stop_reason = ""
    for i in range(1, args.count + 1):
        if available_emails() == 0:
            stop_reason = "邮箱池已空"
            break
        if last_twofa_at is not None and gap > 0:
            wait = gap - (time.time() - last_twofa_at)
            if wait > 0:
                _log("距上一个号 2FA 结束不足 %ss，等待约 %s 分钟后继续" % (int(gap), int(wait // 60) + 1))
                time.sleep(wait)
        if not wait_until_idle(args.idle_wait):
            stop_reason = "节点/队列未空闲"
            break

        _log("===== 第 %s / %s 个 =====" % (i, args.count))
        res = _run_one(i, args)
        results.append(res)
        print("RESULT " + json.dumps(res, ensure_ascii=False), flush=True)
        _log(("完成：" if res["ok"] else "失败：") + "%s" % res["detail"])
        if res["ok"]:
            fail_streak = 0
            last_twofa_at = float(res.get("twofa_finished_at") or time.time())
            continue
        fail_streak += 1
        if fail_streak >= args.max_fail:
            stop_reason = "连续失败 %s 次" % fail_streak
            break
        if i < args.count:
            _log("%ss 后尝试下一个" % int(args.fail_gap))
            time.sleep(args.fail_gap)

    ok_n = sum(1 for r in results if r["ok"])
    _log("===== 汇总 =====")
    for r in results:
        _log("  第 %s 个 | 任务 #%s | %s | 账号 #%s | 出口 %s | %s%s" % (
            r["index"], r["job_id"], r["email"] or "-", r["account_id"] or "-",
            r["proxy_exit"] or "-", "成功" if r["ok"] else "失败",
            ("：" + r["kind"]) if r["kind"] else ""))
    tail = "；提前停止：%s" % stop_reason if stop_reason else ""
    _log("成功 %s / 目标 %s%s" % (ok_n, args.count, tail))
    if not results and stop_reason:
        return EXIT_PREFLIGHT
    return EXIT_OK if ok_n == args.count else EXIT_PARTIAL


# ------------------------------------------------------------ 接管半注册邮箱

# 池备注里的密码：`password=<密码>；`。密码字符集不含空格和分号
_NOTE_PASSWORD_RE = re.compile(r"password=([^；;\s]+)")
_HALF_REGISTERED_MARKS = ("半注册", "已建号")


def list_half_registered() -> list[dict]:
    """邮箱池里标 failed 的半注册邮箱：OpenAI 侧已建号，但邮箱验证/资料页没走完。

    密码从池备注里取（驱动在密码提交之后才失败时会把密码写进备注）；
    取不到的（早期代码没记密码）用一次性验证码登录。
    """
    from core import db

    out = []
    for row in db._load_outlook():
        note = str(row.get("note") or "")
        if str(row.get("status") or "") != "failed" or not any(m in note for m in _HALF_REGISTERED_MARKS):
            continue
        found = _NOTE_PASSWORD_RE.search(note)
        out.append({"email": str(row.get("email") or "").strip(), "pool_id": row.get("id"),
                    "password": found.group(1) if found else "", "note": note[:120]})
    return out


def _mask_exit(proxy: str) -> str:
    try:
        parsed = urlparse(str(proxy or ""))
        return "%s:%s" % (parsed.hostname, parsed.port or "")
    except Exception:  # noqa: BLE001
        return "?"


def _takeover_one(target: dict, args: argparse.Namespace) -> dict:
    """接管一个半注册邮箱：登录补完 → 入库 → 等 2FA → 校验。

    不抛异常：单个失败不影响后面的，原因写在返回的 detail 里。
    """
    import random as _random

    from core.account_export import save_account_data
    from core.ant_registration_bridge import _codex_step, ant_register_flow, pick_ant_proxy
    from core.email_provider import release_email, resolve_email_source
    from core.name_samples import random_display_name

    email = str(target.get("email") or "").strip()
    password = str(target.get("password") or "")
    mode = "password" if password else "otp"
    res = {"email": email, "ok": False, "stage": "login", "mode": mode, "account_id": None,
           "proxy_exit": "", "kind": "", "detail": ""}
    started = time.time()
    proxy = pick_ant_proxy(args.proxy or None)
    res["proxy_exit"] = _mask_exit(proxy)
    _log("接管 %s：%s，出口 %s" % (
        email, "用已有密码登录" if mode == "password" else "没有密码记录，用一次性验证码登录",
        res["proxy_exit"]))
    try:
        info = ant_register_flow(
            email, proxy=proxy, password=password, full_name=random_display_name(),
            # 年龄取大一些：偏小时资料页会多一个出生日期确认框，接管没必要去碰它
            age=_random.randint(28, 48), takeover=mode)
    except Exception as exc:  # noqa: BLE001
        res.update(kind=type(exc).__name__, detail=str(exc)[:240])
        return res

    res["stage"] = "save"
    _log("登录并补完：%s（用时 %ss），入库" % (email, int(time.time() - started)))
    extra = {
        "user": info.get("user") or {"id": info.get("user_id"), "name": info.get("user_name")},
        "account": info.get("account"),
        "expires": info.get("expires"),
        "ant_browser": {"profile_id": info.get("profile_id")},
        "codex": _codex_step(email, proxy),
        "takeover": {"mode": mode, "at": datetime.now().isoformat(timespec="seconds")},
    }
    if password:
        extra["registration_password"] = password
    try:
        # save_account_data → insert_account 会顺带把邮箱池这一行从 failed 改成 used，
        # 并记上 account_id 和完成时间
        row_id = save_account_data(email=email, access_token=info.get("access_token") or "",
                                   extra=extra, email_source=resolve_email_source(email),
                                   proxy_used=proxy)
    except Exception as exc:  # noqa: BLE001
        res.update(kind="save_failed",
                   detail="登录成功但入库失败：%s: %s" % (type(exc).__name__, str(exc)[:160]))
        return res
    res["account_id"] = row_id
    # 备注换成接管记录，免得以后还被当成「待接管」
    try:
        release_email(email, status="used",
                      note="已接管入库：账号 #%s（%s登录）" % (row_id, "密码" if mode == "password" else "一次性验证码"))
    except Exception as exc:  # noqa: BLE001
        _log("更新邮箱池备注失败（不影响结果）：%s" % str(exc)[:100])
    return _wait_twofa_and_verify(res, args, started=started, require_password=bool(password))


def run_takeover(args: argparse.Namespace) -> int:
    targets = list_half_registered()
    if args.email:
        by_email = {t["email"].lower(): t for t in targets}
        picked, skipped = [], []
        for raw in args.email:
            key = str(raw or "").strip().lower()
            if key in by_email:
                picked.append(by_email[key])
            else:
                skipped.append(str(raw))
        if skipped:
            # 不在半注册清单里的邮箱不碰：可能是正常的可用邮箱，拿去登录会把注册流程搅乱
            _log("不在半注册清单里（状态不是 failed，或备注不是半注册），跳过：%s" % "、".join(skipped))
        targets = picked
    if args.password:
        for target in targets:
            target["password"] = args.password

    if not targets:
        _log("没有可接管的半注册邮箱")
        return EXIT_OK
    _log("半注册邮箱 %s 个：" % len(targets))
    for target in targets:
        _log("  %s（池 #%s，%s）" % (target["email"], target.get("pool_id"),
                                 "有密码记录" if target["password"] else "无密码记录"))
    if args.list:
        return EXIT_OK

    # 接管在本进程里直接跑页面流程：把流程自己的日志打到屏幕上，
    # 否则只看得到这个脚本的几行打印，卡在哪一步无从判断
    import logging
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            datefmt="%H:%M:%S")

    checks = collect_preflight(takeover=True)
    print_preflight(checks)
    hard = [c["name"] for c in checks if not c["ok"] and c["name"] not in _WAITABLE]
    if hard:
        _log("预检未通过，一个邮箱都没处理：%s" % "、".join(hard))
        return EXIT_PREFLIGHT
    if not wait_until_idle(args.idle_wait):
        _log("节点/队列未空闲，一个邮箱都没处理")
        return EXIT_PREFLIGHT

    results: list[dict] = []
    fail_streak = 0
    stop_reason = ""
    for i, target in enumerate(targets, 1):
        if i > 1 and not wait_until_idle(args.idle_wait):
            stop_reason = "节点/队列未空闲"
            break
        _log("===== 第 %s / %s 个 =====" % (i, len(targets)))
        res = _takeover_one(target, args)
        res["index"] = i
        results.append(res)
        print("RESULT " + json.dumps(res, ensure_ascii=False), flush=True)
        _log(("完成：" if res["ok"] else "失败：") + res["detail"])
        if res["ok"]:
            fail_streak = 0
            continue
        fail_streak += 1
        if fail_streak >= args.max_fail:
            stop_reason = "连续失败 %s 次" % fail_streak
            break

    ok_n = sum(1 for r in results if r["ok"])
    _log("===== 汇总 =====")
    for r in results:
        _log("  第 %s 个 | %s | 账号 #%s | 出口 %s | %s%s" % (
            r["index"], r["email"], r["account_id"] or "-", r["proxy_exit"] or "-",
            "成功" if r["ok"] else "失败", ("：" + r["kind"]) if r["kind"] else ""))
    tail = "；提前停止：%s" % stop_reason if stop_reason else ""
    _log("接管成功 %s / 目标 %s%s" % (ok_n, len(targets), tail))
    all_ok = ok_n == len(targets) and not stop_reason
    return EXIT_OK if all_ok else EXIT_PARTIAL


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ant Browser 注册自动化（经 WebUI 任务接口，逐个串行）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="只做预检并打印结果，不提交任务")
    run_p = sub.add_parser("run", help="按需批量注册")
    run_p.add_argument("--count", type=int, default=1, help="要注册的账号数（默认 1）")
    run_p.add_argument("--max-fail", type=int, default=2,
                       help="连续失败多少次就停（默认 2；每次失败都可能消耗一个池邮箱）")
    run_p.add_argument("--gap", type=float, default=None,
                       help="两个号之间的间隔秒数，从上一个号 2FA 结束算起；默认取 AUTO_REGISTER_INTERVAL，0 表示不额外等待")
    run_p.add_argument("--fail-gap", type=float, default=120.0, help="失败后等多久再试下一个（默认 120s）")
    run_p.add_argument("--idle-wait", type=float, default=1800.0,
                       help="节点/队列被占用时最多等多久（默认 1800s）")
    run_p.add_argument("--job-timeout", type=float, default=1800.0, help="单个注册任务最长等多久（默认 1800s）")
    run_p.add_argument("--twofa-queue-wait", type=float, default=120.0,
                       help="注册成功后等 2FA 入队最多久（默认 120s）")
    run_p.add_argument("--twofa-timeout", type=float, default=1800.0, help="2FA 入队后最长等多久（默认 1800s）")

    tk = sub.add_parser("takeover", help="接管半注册邮箱（OpenAI 侧已建号，验证码/资料页没走完）")
    tk.add_argument("--list", action="store_true", help="只列出可接管的半注册邮箱，不执行")
    tk.add_argument("--all", action="store_true", help="接管清单里的全部（不带 --email 时必须）")
    tk.add_argument("--email", action="append", default=[],
                    help="只接管指定邮箱，可重复给出；不在半注册清单里的会被跳过")
    tk.add_argument("--password", default="",
                    help="登录密码；缺省从池备注里取，取不到就用一次性验证码登录")
    tk.add_argument("--proxy", default="", help="指定代理出口；缺省从节点可达的出口里随机抽")
    tk.add_argument("--max-fail", type=int, default=2, help="连续失败多少次就停（默认 2）")
    tk.add_argument("--idle-wait", type=float, default=1800.0,
                    help="节点/队列被占用时最多等多久（默认 1800s）")
    tk.add_argument("--twofa-queue-wait", type=float, default=120.0,
                    help="入库后等 2FA 入队最多久（默认 120s）")
    tk.add_argument("--twofa-timeout", type=float, default=1800.0, help="2FA 入队后最长等多久（默认 1800s）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "preflight":
        checks = collect_preflight()
        print_preflight(checks)
        return EXIT_OK if all(c["ok"] for c in checks) else EXIT_PARTIAL
    if args.command == "takeover":
        if not args.list and not args.email and not args.all:
            _log("请用 --list 只查看，或用 --all / --email 指定要接管的邮箱")
            return EXIT_PREFLIGHT
        if args.max_fail < 1:
            args.max_fail = 1
        return run_takeover(args)
    if args.count < 1:
        _log("--count 必须 >= 1")
        return EXIT_PREFLIGHT
    if args.max_fail < 1:
        args.max_fail = 1
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _log("已中断。已提交的注册任务不受影响，会在 WebUI 主进程里继续跑完。")
        sys.exit(130)
