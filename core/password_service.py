# -*- coding: utf-8 -*-
"""账号补密码后台队列：登录+reauth 给无密码账号补设 ChatGPT 登录密码。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import email as _email_cfg
from config import password as _pwd_cfg
from core import db
from core.openai_auth import generate_registration_password
from core.session import close_browser_session
from core.proxy_utils import mask_proxy_url

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(_pwd_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_MAX_WORKERS = 8
_WORKERS = _int_setting("PASSWORD_BACKFILL_WORKERS", 2, 1, _MAX_WORKERS)
_QUEUE_LIMIT = _int_setting("PASSWORD_BACKFILL_QUEUE_LIMIT", 100, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="pwd-backfill")
_RUNNING: set[int] = set()
_OUTSTANDING = 0
_LOCK = threading.Condition()
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def apply_settings() -> dict:
    """Apply freshly loaded config values to the live queue service."""
    global _WORKERS, _QUEUE_LIMIT
    workers = _int_setting("PASSWORD_BACKFILL_WORKERS", 2, 1, _MAX_WORKERS)
    queue_limit = _int_setting("PASSWORD_BACKFILL_QUEUE_LIMIT", 100, workers, 5000)
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
    return _LOG_DIR / f"password-{safe}.log"


def _normalize_proxy(proxy: str | None) -> str | None:
    """入口只接受真实代理地址；环境标签（如 skyvern:jp）不是可用代理。"""
    text = str(proxy or "").strip()
    if not text:
        return None
    low = text.lower()
    if low.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")):
        return text
    return None


def _target_proxy(proxy: str | None) -> str | None:
    """按 PASSWORD_BACKFILL_PROXY_MODE 决定传给登录链的目标代理。

    saved：账号保存的代理（不可用则回落代理池）；pool：显式 None 交给
    BrowserSession 从代理池选择。登录链内部自行处理代理链式中继。
    """
    mode = str(getattr(_pwd_cfg, "PASSWORD_BACKFILL_PROXY_MODE", "saved") or "saved").strip().lower()
    if mode not in {"saved", "pool"}:
        raise ValueError(f"PASSWORD_BACKFILL_PROXY_MODE={mode!r} 无效，可选 saved / pool")
    if mode == "pool":
        return None
    return _normalize_proxy(proxy)


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


def _otp_waiter(email: str, email_source: str | None):
    """构造 reauth 验证码等待函数：复用统一取码分发。"""
    from core.email_provider import wait_for_otp

    max_wait = max(30, int(getattr(_pwd_cfg, "PASSWORD_BACKFILL_EMAIL_TIMEOUT", 120) or 120))

    def _wait(after_ts: float) -> str:
        return wait_for_otp(
            email,
            after_ts=after_ts,
            max_wait=max_wait,
            email_source=email_source,
            force_service=True,
        )

    return _wait


def _verify_password_login(
    email: str,
    email_source: str | None,
    proxy: str | None,
) -> dict:
    """用"密码+TOTP"完整登录一次，验证补设的密码确实生效。

    密码已先落库，_login_via_password_or_otp 会自动读到并优先走密码登录，
    完全绕开邮箱 OTP。返回 {ok, access_token?, token_expires_at?, error?}。
    """
    from core.account_liveness import _login_via_full_web_flow

    session = None
    try:
        session, info = _login_via_full_web_flow(
            email, proxy, email_source=email_source, fingerprint_state={},
        )
        token = str(info.get("accessToken") or "")
        if not token:
            return {"ok": False, "error": "密码登录成功但没有拿到 accessToken"}
        return {
            "ok": True,
            "access_token": token,
            "token_expires_at": info.get("expires"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if session is not None:
            try:
                close_browser_session(session)
            except Exception:  # noqa: BLE001
                pass


def _run_password_backfill(
    *, account_id: int, email: str, proxy: str | None, email_source: str | None,
    trigger: str,
) -> dict:
    global _OUTSTANDING
    fh: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    try:
        with _LOCK:
            while len(_RUNNING) >= _WORKERS:
                _LOCK.wait()
            _RUNNING.add(int(account_id))
        if not db.mark_account_password_backfill_running(account_id):
            return {"ok": False, "status": "failed", "error": "账号已删除或补密码状态已被重置"}
        log_file = log_path(email)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("", encoding="utf-8")
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)
        logger.info("[补密码] 开始后台执行：email=%s trigger=%s", email, trigger)

        target_proxy = _target_proxy(proxy)
        _append_log(
            email,
            f"[补密码] 网络出口：target={mask_proxy_url(target_proxy) or '代理池随机'} "
            f"mode={getattr(_pwd_cfg, 'PASSWORD_BACKFILL_PROXY_MODE', 'saved')}",
        )

        from core.password_reset import run_password_backfill

        new_password = generate_registration_password(14)
        session, flow_result = run_password_backfill(
            email,
            proxy=target_proxy,
            new_password=new_password,
            otp_waiter=_otp_waiter(email, email_source),
            email_source=email_source,
        )

        # 密码先落库：后面的验证登录要从库里读到密码才能走密码分支。
        db.update_account_password_backfill(
            account_id,
            {"ok": True, "status": "success", "password": new_password,
             "message": "密码已设置", "checked_at": datetime.now().isoformat(timespec="seconds")},
        )
        _append_log(email, "[补密码] OpenAI 已接受新密码，先落库再验证登录")
        logger.info("[补密码] 密码已落库：email=%s", email)

        verify_note = "未开启验证登录"
        if bool(getattr(_pwd_cfg, "PASSWORD_BACKFILL_VERIFY_LOGIN", True)):
            verify = _verify_password_login(email, email_source, target_proxy)
            if verify.get("ok"):
                verify_note = "验证登录成功，已刷新 accessToken"
                db.update_account_password_backfill(
                    account_id,
                    {"ok": True, "status": "success", "password": new_password,
                     "access_token": verify.get("access_token"),
                     "token_expires_at": verify.get("token_expires_at"),
                     "message": "密码已设置并通过登录验证",
                     "checked_at": datetime.now().isoformat(timespec="seconds")},
                )
            else:
                # 密码本身已设置成功；验证失败只提示，不改 success 状态。
                verify_note = f"验证登录失败（不影响密码已生效）：{verify.get('error')}"
        _append_log(email, f"[补密码] {verify_note}")
        logger.info("[补密码] 完成：email=%s %s", email, verify_note)
        return {"ok": True, "status": "success", "password": new_password, "message": verify_note}
    except Exception as exc:
        result = {"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        try:
            db.update_account_password_backfill(account_id, result)
        except Exception:
            logger.exception("[补密码] 写回失败状态失败: account_id=%s", account_id)
        try:
            _append_log(email, f"[补密码] 失败：{result['error']}")
        except Exception:
            pass
        logger.exception("[补密码] 后台异常: %s", email)
        return result
    finally:
        if fh is not None:
            try:
                root_logger.removeHandler(fh)
                fh.close()
            except Exception:
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


def enqueue_account_password_backfill(
    *, account_id: int, email: str, trigger: str = "manual",
    proxy: str | None = None, email_source: str | None = None,
) -> dict:
    global _OUTSTANDING
    apply_settings()
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if str(email_source or "").strip().lower() not in {"outlook", ""}:
        return {"accepted": False, "busy": False, "error": "补密码暂只支持 outlook/hotmail 邮箱"}
    if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
        return {"accepted": False, "busy": False, "error": "补密码需要先开启 USE_EMAIL_SERVICE 自动收取邮箱验证码"}
    with _LOCK:
        if _OUTSTANDING >= _QUEUE_LIMIT:
            return {"accepted": False, "busy": False, "queue_full": True, "error": "补密码队列已满，请稍后重试"}
        _OUTSTANDING += 1
    if not db.claim_account_password_backfill(acc_id=account_id, trigger=trigger):
        _release_outstanding()
        return {"accepted": False, "busy": True, "error": "该账号正在补密码"}

    _append_log(email, f"[补密码] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        future = _EXECUTOR.submit(
            _run_password_backfill,
            account_id=account_id,
            email=email,
            proxy=proxy,
            email_source=email_source,
            trigger=str(trigger or "manual"),
        )
        return {"accepted": True, "busy": False, "future": future, "log_path": str(log_path(email))}
    except Exception as exc:
        _release_outstanding()
        db.update_account_password_backfill(
            account_id, {"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return {"accepted": False, "busy": False, "error": f"{type(exc).__name__}: {exc}"}
