# -*- coding: utf-8 -*-
"""更换已开启的 2FA（TOTP）密钥。

为什么单独一个模块：twofa_service.setup_2fa 是"首次设置"流程，
走邮箱 OTP 重认证——akmoz 导入的这批账号邮箱取不到码（404），
那条路走不通。2026-11-19 实测（#149/#155）发现另一条可行路径：

  1. 用旧 TOTP 完整登录（密码/OTP + MFA），拿到"刚登录"的新鲜 AT；
  2. recent_auth 窗口内直接 POST /backend-api/accounts/mfa/enroll，
     已有 TOTP 的账号也允许 enroll（会返回新 secret）；
  3. 用新 secret 生成验证码 POST .../activate_enrollment 激活。

关键点：
  - 旧 token 超出 recent_auth 窗口会被 401 recent_auth_required 拒绝，
    所以必须"登录后立即 enroll"，不能拿存量 AT 直接调。
  - enroll 返回新 secret，激活成功后旧 secret 自动作废。
  - 失败的安全边界：enroll 之前失败不碰旧密钥；activate 失败时旧密钥
    是否保留未实测过，所以旧密钥始终先备份到 cache 目录再动手。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from core import db

logger = logging.getLogger(__name__)

ENROLL_URL = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
ACTIVATE_URL = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"

_lock = threading.Lock()
_running: set[int] = set()


def is_running(account_id: int) -> bool:
    with _lock:
        return int(account_id) in _running


def _backup_old_secret(account_id: int, email: str, secret: str) -> Path:
    """换密钥前先把旧密钥落盘；激活失败时这是唯一的回滚线索。"""
    path = Path("/app/cache/totp_backups")
    path.mkdir(parents=True, exist_ok=True)
    fp = path / f"totp_backup_{int(account_id)}.json"
    fp.write_text(json.dumps({
        "account_id": int(account_id), "email": email, "old_totp": secret,
        "backup_at": db._now(),
    }, ensure_ascii=False), encoding="utf-8")
    return fp


def _fresh_login(account_id: int) -> tuple[str, str]:
    """完整登录拿"刚登录"的新鲜 AT（B 方案的核心前提）。

    返回 (新 access_token, 使用的出口)。出口按顺序尝试，403/超时换下一个。
    登录路径由 account_liveness 决定（有密码走密码+TOTP，没有走邮箱 OTP）。
    """
    from core.account_liveness import check_account_liveness
    from config.proxy import normalize_proxy_url, proxy_pool_entries
    from webui.config_editor import get_field_value

    acc = db.get_account(int(account_id)) or {}
    email = str(acc.get("email") or "")
    source = str(acc.get("email_source") or "outlook")

    raw = get_field_value("PROXY_POOL") or []
    cands, seen = [], set()
    for e in proxy_pool_entries(raw):
        u = normalize_proxy_url(str(e.get("url") or "").strip())
        if u and u not in seen and e.get("enabled"):
            seen.add(u)
            cands.append((str(e.get("name") or "").strip() or u.split("@")[-1], u))

    tried: list[str] = []
    for name, proxy in cands:
        tried.append(proxy)
        try:
            r = check_account_liveness(email, proxy=proxy, email_source=source)
        except Exception as exc:  # noqa: BLE001 - 换下一个出口
            logger.warning("[换密钥] %s 登录异常（%s）：%s: %s",
                           email, name, type(exc).__name__, str(exc)[:120])
            continue
        if r.get("ok"):
            db.update_account_liveness(int(account_id), r)
            tok = str(r.get("access_token") or "")
            if tok:
                logger.info("[换密钥] %s 登录成功（%s）", email, name)
                return tok, proxy
        err = str(r.get("error") or "")
        # 403 是出口被限流，换下一个；账号被废（deactivated）换出口也没用
        if r.get("status") == "deactivated":
            raise RuntimeError("账号已被停用，无法登录更换密钥")
        if "403" not in err:
            # OTP 超时之类再等一轮出口也没意义，直接报出真实原因
            raise RuntimeError(f"登录失败：{err[:200]}")
        logger.warning("[换密钥] %s 出口 %s 被限流，换下一个", email, name)
    raise RuntimeError(f"所有出口都无法登录（尝试 {len(tried)} 个）")


def _enroll_and_activate(proxy: str, fresh_token: str) -> str:
    """用刚登录的 AT enroll 新 TOTP 并激活，返回新 secret。"""
    from core.session import BrowserSession
    from config.proxy import normalize_proxy_url

    env = BrowserSession(proxy=normalize_proxy_url(proxy),
                         fingerprint_seed=f"totp-rotate:{fresh_token[:16]}")
    try:
        try:
            env.get("https://chatgpt.com/",
                    headers=env.get_chatgpt_navigate_headers(
                        referer="https://chatgpt.com/", user_initiated=False),
                    allow_redirects=True, timeout=20)
        except Exception:  # noqa: BLE001 - 预热失败不挡 enroll
            pass
        env.blocked_until = 0.0
        env.blocked_reason = ""

        h = env.get_chatgpt_headers(referer="https://chatgpt.com/")
        h["authorization"] = f"Bearer {fresh_token}"
        h["oai-device-id"] = env.device_id
        h["oai-language"] = env.navigator_language()
        h["content-type"] = "application/json"

        resp = env.post(ENROLL_URL, headers=h,
                        data=json.dumps({"factor_type": "totp"}), timeout=30)
        body = (resp.text or "")[:300]
        if resp.status_code != 200:
            raise RuntimeError(f"enroll 被拒 HTTP {resp.status_code}: {body}")
        data = json.loads(resp.text or "{}")
        secret = str(data.get("secret") or "")
        session_id = str(data.get("session_id") or "")
        if not secret or not session_id:
            raise RuntimeError(f"enroll 响应缺少 secret/session_id: {body}")

        # 激活前稍等一下，避免连续请求触发风控
        time.sleep(2)
        import pyotp  # 延迟导入：本地跑单测的环境不一定装了它
        code = pyotp.TOTP(secret).now()
        resp2 = env.post(ACTIVATE_URL, headers=h,
                         data=json.dumps({"code": code, "factor_type": "totp",
                                          "session_id": session_id}),
                         timeout=30)
        body2 = (resp2.text or "")[:300]
        if resp2.status_code != 200:
            raise RuntimeError(f"激活被拒 HTTP {resp2.status_code}: {body2}")
        ok2 = json.loads(resp2.text or "{}").get("success")
        if not ok2:
            raise RuntimeError(f"激活返回 success=false: {body2}")
        return secret
    finally:
        try:
            env.close()
        except Exception:  # noqa: BLE001
            pass


def rotate_totp_secret(account_id: int, *, trigger: str = "manual") -> dict:
    """更换账号的 2FA 密钥；成功返回 {"ok": True, "new_secret": ...}。

    串行约束：同一账号同时只允许一个更换任务（和 2FA 设置的互斥类似，
    但这里是独立集合，避免和首次设置的队列互相挤占）。
    """
    account_id = int(account_id)
    acc = db.get_account(account_id) or {}
    email = str(acc.get("email") or "")
    old_secret = str(acc.get("totp_secret") or "")

    if not email:
        return {"ok": False, "error": "账号不存在"}
    if not old_secret:
        return {"ok": False, "error": "该账号没有已启用的 2FA，无需更换（请用「开启2FA」）"}

    with _lock:
        if account_id in _running:
            return {"ok": False, "error": "该账号正在更换密钥，请勿重复触发"}
        _running.add(account_id)
    try:
        backup = _backup_old_secret(account_id, email, old_secret)
        logger.info("[换密钥] %s 开始（旧密钥已备份 %s）", email, backup.name)

        fresh_token, proxy = _fresh_login(account_id)
        logger.info("[换密钥] %s 已拿到新鲜登录态，立即 enroll", email)
        new_secret = _enroll_and_activate(proxy, fresh_token)

        with db._LOCK:
            rows = db._load_accounts()
            for r in rows:
                if int(r.get("id") or 0) == account_id:
                    r["totp_secret"] = new_secret
                    r["totp_setup_status"] = "success"
                    r["totp_setup_ok"] = True
                    r["totp_setup_error"] = None
                    r["totp_setup_trigger"] = f"rotate:{trigger}"
                    r["totp_setup_completed_at"] = db._now()
                    # 登录拿到的 fresh token 一并落库，比旧 AT 更新
                    r["access_token"] = fresh_token
                    r["updated_at"] = db._now()
                    break
            db._save_accounts(rows)
        logger.info("[换密钥] %s 完成：新密钥 %s...%s",
                    email, new_secret[:4], new_secret[-4:])
        return {"ok": True, "new_secret": new_secret,
                "backup": str(backup), "email": email}
    except Exception as exc:  # noqa: BLE001 - 失败必须带上下文返回
        err = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.warning("[换密钥] %s 失败：%s", email, err)
        return {"ok": False, "error": err, "email": email}
    finally:
        with _lock:
            _running.discard(account_id)
