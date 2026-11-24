# -*- coding: utf-8 -*-
"""OpenAI 补设密码协议流程：登录 → reauth → /password/add。

逆向自 auth.openai.com SPA（2026-11-19 实测校准）：

  1. 完整登录（邮箱 OTP；有 TOTP 的账号自动过 MFA）——复用查活登录链
     ``account_liveness._login_via_full_web_flow``。
  2. chatgpt.com 发起 reauth（同 2FA 流程的 ``_trigger_reauth``），
     callbackUrl action=add_password；authorize 落到 /email-verification
     并发送 reauth OTP 邮件。
  3. ``POST /api/accounts/email-otp/validate`` 校验 reauth 验证码。
  4. ``POST /api/accounts/password/add`` body {"password": ...}
     （sentinel flow=password_reset，最少 12 位）。

已证死路（勿回头）：
  - 登录页"忘记密码"``POST /api/accounts/password/send-otp`` 对无密码账号
    恒返回 ``invalid_auth_step``（identifier 提交后状态机停在 email_otp，
    密码页重置仅对已有密码账号开放）；带/不带导航、不提交邮箱都一样。
  - 登录完成后直接 ``/password/add`` 同样 ``invalid_auth_step``——auth
    session 已随 OAuth 回调消费掉，必须经由 reauth 重建。
"""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlencode

from core.codex_oauth import _post_json, _resp_json
from core.openai_auth import (
    build_sentinel_header,
    request_sentinel_token,
    validate_email_otp,
    EmailOtpInvalidError,
)
from core.session import BrowserSession, close_browser_session

logger = logging.getLogger(__name__)

# 重置密码的新密码要求：至少 12 位（SPA 校验 minLength=12）。
PASSWORD_MIN_LENGTH = 12

# password/add 失败码（SPA errorResponses）→ 可读原因。
_ADD_ERROR_HINTS = {
    "password_too_weak": "密码强度不足",
    "reused_password": "不能复用旧密码",
    "password_contains_user_info": "密码不能包含邮箱/姓名等个人信息",
    "add_password_ineligible": "该账号不符合添加密码的条件（可能被工作区 SSO 管控）",
    "invalid_auth_step": "auth 会话不在添加密码状态（reauth 未生效或 action 参数不被识别）",
    "invalid_state": "auth 会话已失效，需要重新发起",
    "sensitive_action_cooldown": "操作过于频繁被限流，请稍后再试",
    "rate_limit_exceeded": "触发限流，请稍后再试",
}


def _log_prefix(email: str) -> str:
    return f"[补密码] {email}"


def _friendly_add_error(status: int, body: str) -> str:
    low = (body or "").lower()
    for code, hint in _ADD_ERROR_HINTS.items():
        if code in low:
            return f"{hint}（{code}）"
    return f"password/add 失败 status={status}: {body[:240]}"


def _decode_auth_session(session: BrowserSession) -> dict:
    """从会话 cookie 解出 oai-client-auth-session 第一段（诊断用）。"""
    raw = ""
    try:
        jar = getattr(getattr(session, "session", None), "cookies", None)
        if jar is not None:
            try:
                raw = jar.get("oai-client-auth-session") or ""
            except Exception:
                for c in jar:
                    if c.name == "oai-client-auth-session":
                        raw = c.value or ""
                        break
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    import base64

    try:
        seg = raw.split(".")[0]
        seg += "=" * (-len(seg) % 4)
        data = json.loads(base64.urlsafe_b64decode(seg))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def trigger_add_password_reauth(session: BrowserSession, email: str, action: str = "add_password") -> float:
    """发起 reauth 并触发 reauth OTP 邮件；返回 OTP 的 after_ts。

    照抄 2FA 流程的 ``_trigger_reauth``（chatgpt.com signin + reauth=password），
    仅把 callbackUrl 的 action 换成补密码入口。
    """
    prefix = _log_prefix(email)
    csrf_resp = session.get(
        "https://chatgpt.com/api/auth/csrf",
        headers=session.get_nextauth_headers(referer="https://chatgpt.com/"),
    )
    csrf_resp.raise_for_status()
    csrf_token = csrf_resp.json()["csrfToken"]

    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": session.device_id,
    }
    body = urlencode({
        "callbackUrl": f"https://chatgpt.com/?action={action}",
        "csrfToken": csrf_token,
        "json": "true",
    })
    resp = session.post(
        "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query),
        headers={
            **session.get_nextauth_headers(referer="https://chatgpt.com/"),
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://chatgpt.com",
        },
        data=body,
    )
    resp.raise_for_status()
    auth_url = (resp.json() or {}).get("url") or ""
    if not auth_url:
        raise RuntimeError(f"{prefix} reauth 未返回 authorize URL: {(resp.text or '')[:200]}")

    after_ts = time.time()
    nav = session.get(
        auth_url,
        headers=session.get_auth_navigate_headers(referer="https://chatgpt.com/"),
        allow_redirects=True,
    )
    final_url = str(getattr(nav, "url", "") or "")
    logger.info("%s reauth authorize 落点：%s", prefix, final_url[:120])
    if "/email-verification" not in final_url:
        raise RuntimeError(f"{prefix} reauth 落点异常（预期 /email-verification）：{final_url[:200]}")
    return after_ts


def submit_new_password(session: BrowserSession, email: str, new_password: str, *, endpoint: str = "add") -> dict:
    """提交新密码；返回 password/add|reset 响应 JSON。"""
    prefix = _log_prefix(email)
    sentinel_resp = request_sentinel_token(session, "password_reset")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "password_reset")
    resp = _post_json(
        session,
        f"https://auth.openai.com/api/accounts/password/{endpoint}",
        {"password": new_password},
        referer="https://auth.openai.com/reset-password/new-password",
        sentinel_header=sentinel_header,
        so_header=so_header,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(_friendly_add_error(resp.status_code, resp.text or ""))
    data = _resp_json(resp)
    logger.info(
        "%s 新密码已提交(page=%s) 会话标志=%s",
        prefix, (data.get("page") or {}).get("type") or "-",
        sorted(_decode_auth_session(session).keys())[:8],
    )
    return data


def run_password_backfill(
    email: str,
    *,
    proxy: str | None,
    new_password: str,
    otp_waiter,
    email_source: str | None = None,
    fingerprint_seed: str | None = None,
) -> tuple[BrowserSession, dict]:
    """完整补密码流程；成功返回 (登录会话, {"password", "add"})。

    ``otp_waiter(after_ts) -> str`` 负责等待 reauth 验证码邮件（同一等待器
    也会被登录链内部使用）。会话的关闭责任归调用方。
    """
    from core.account_liveness import _login_via_full_web_flow

    prefix = _log_prefix(email)
    if len(new_password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"新密码至少 {PASSWORD_MIN_LENGTH} 位")

    session, info = _login_via_full_web_flow(
        email, proxy, email_source=email_source, fingerprint_state={},
    )
    try:
        logger.info("%s 登录完成，开始 reauth 发起补密码", prefix)
        after_ts = trigger_add_password_reauth(session, email)

        code = otp_waiter(after_ts)
        if not code:
            raise RuntimeError("未取到 reauth 验证码")
        logger.info("%s 已取到 reauth 验证码", prefix)

        validate_result = validate_email_otp(session, code)
        logger.info(
            "%s reauth 验证码校验通过 page=%s continue=%s",
            prefix, (validate_result.get("page") or {}).get("type") or "-",
            validate_result.get("continue_url") or "-",
        )
        add_result = submit_new_password(session, email, new_password)
        return session, {"password": new_password, "add": add_result}
    except BaseException:
        try:
            close_browser_session(session)
        except Exception:  # noqa: BLE001
            pass
        raise


__all__ = [
    "PASSWORD_MIN_LENGTH",
    "EmailOtpInvalidError",
    "trigger_add_password_reauth",
    "submit_new_password",
    "run_password_backfill",
]
