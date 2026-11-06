# -*- coding: utf-8 -*-
"""把跑通 Codex 的账号导入 sub2api。

sub2api 只接受 oauth / apikey 两种账号类型，不收 agent_identity
（实测 type=agent_identity 直接 400）。好在 Codex 授权的产物里同时带着
access_token / refresh_token，能拼成 sub2api 要的 oauth 凭据，和它现有的
AG*-Team1 账号格式一致，可以正常调度。

命名：AG + 订阅编号 + "-Team1"，周限额编号前面加 Z。
    5h 限额编号 49  -> AG49-Team1
    周限额编号 8    -> AGZ8-Team1
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# 固定文本，和 sub2api 里已有账号保持一致
NAME_SUFFIX = "-Team1"
# 目前所有订阅都是埃及区
REGION_PREFIX = "AG"


class Sub2apiError(RuntimeError):
    """导入失败。"""


def _cfg(name: str, default=None):
    from config import sub2api as c
    return getattr(c, name, default)


def _base_url() -> str:
    url = str(_cfg("SUB2API_API_BASE", "") or "").strip().rstrip("/")
    if not url:
        raise Sub2apiError("SUB2API_API_BASE 没配置")
    return url


def _request(method: str, path: str, *, token: str = "",
             payload: dict | None = None, timeout: float = 25.0) -> dict:
    url = _base_url() + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise Sub2apiError(f"HTTP {exc.code}: {body[:200]}") from None
    except Exception as exc:
        raise Sub2apiError(f"{type(exc).__name__}: {exc}") from None
    try:
        out = json.loads(body)
    except Exception:
        raise Sub2apiError(f"响应不是 JSON：{body[:200]}") from None
    if out.get("code") not in (0, None):
        raise Sub2apiError(str(out.get("message") or out)[:200])
    return out


def login() -> str:
    """用管理员账号换 JWT。sub2api 没有长期 API Key，只能每次登录。"""
    email = str(_cfg("SUB2API_ADMIN_EMAIL", "") or "").strip()
    password = str(_cfg("SUB2API_ADMIN_PASSWORD", "") or "")
    if not email or not password:
        raise Sub2apiError("SUB2API_ADMIN_EMAIL / SUB2API_ADMIN_PASSWORD 没配置")
    out = _request("POST", "/api/v1/auth/login",
                   payload={"email": email, "password": password})
    token = str(((out.get("data") or {}).get("access_token")) or "").strip()
    if not token:
        raise Sub2apiError("登录成功但没拿到 access_token")
    return token


def _jwt_claims(token: str) -> dict:
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def build_account_name(subscription_no, subscription_kind: str) -> str:
    """AG49-Team1 / AGZ8-Team1。没有编号的账号不该走导入。"""
    if not subscription_no:
        raise Sub2apiError("账号没有订阅编号，无法命名")
    mark = "Z" if str(subscription_kind or "") == "weekly" else ""
    return f"{REGION_PREFIX}{mark}{int(subscription_no)}{NAME_SUFFIX}"


def build_credentials(codex_payload: dict) -> dict:
    """把 codex_accounts 里那份 auth.json 转成 sub2api 的 oauth 凭据。

    身份信息藏在 id_token 的 OpenAI 命名空间里，顶层字段不够用。
    """
    access_token = str(codex_payload.get("access_token") or "").strip()
    refresh_token = str(codex_payload.get("refresh_token") or "").strip()
    id_token = str(codex_payload.get("id_token") or "").strip()
    if not access_token or not refresh_token:
        raise Sub2apiError("codex 数据缺少 access_token / refresh_token")
    if not id_token:
        raise Sub2apiError("codex 数据缺少 id_token，拿不到账号身份信息")

    claims = _jwt_claims(id_token)
    auth = claims.get("https://api.openai.com/auth") or {}
    aud = claims.get("aud")
    client_id = aud[0] if isinstance(aud, list) and aud else aud

    orgs = auth.get("organizations") or []
    org_id = ""
    if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict):
        org_id = str(orgs[0].get("id") or "")

    return {
        "client_id": str(client_id or ""),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "chatgpt_user_id": str(auth.get("chatgpt_user_id") or ""),
        "chatgpt_account_id": str(auth.get("chatgpt_account_id")
                                  or codex_payload.get("account_id") or ""),
        "organization_id": org_id,
        "email": str(claims.get("email") or codex_payload.get("email") or ""),
        "plan_type": str(auth.get("chatgpt_plan_type") or "team"),
        "expires_at": str(codex_payload.get("expired") or ""),
        "subscription_expires_at": str(
            auth.get("chatgpt_subscription_active_until") or ""),
    }


def find_proxy_id(token: str, proxy_name: str) -> int:
    """按名字找代理。sub2api 的代理名和我们代理池一致，所以能直接匹配。"""
    name = str(proxy_name or "").strip()
    if not name:
        return 0
    out = _request("GET", "/api/v1/admin/proxies?page=1&page_size=200",
                   token=token)
    items = (out.get("data") or {}).get("items") or []
    for p in items:
        if str(p.get("name") or "").strip().lower() == name.lower():
            return int(p.get("id") or 0)
    return 0


def find_account_by_name(token: str, name: str) -> dict | None:
    out = _request("GET", "/api/v1/admin/accounts?page=1&page_size=200",
                   token=token)
    items = (out.get("data") or {}).get("items") or []
    for a in items:
        if str(a.get("name") or "") == name:
            return a
    return None


def import_account(
    *,
    codex_payload: dict,
    subscription_no,
    subscription_kind: str,
    proxy_name: str = "",
    group_ids: list[int] | None = None,
) -> dict:
    """导入一个账号，返回 {name, account_id, proxy, groups, updated}。

    同名账号存在时更新而不是重复建——重复建会让 sub2api 调度到失效凭据。
    """
    token = login()
    name = build_account_name(subscription_no, subscription_kind)
    creds = build_credentials(codex_payload)
    groups = list(group_ids or _cfg("SUB2API_GROUP_IDS", [6]) or [6])

    proxy_id = find_proxy_id(token, proxy_name) if proxy_name else 0
    if proxy_name and not proxy_id:
        logger.warning("[sub2api] 代理池里有 %s，但 sub2api 里没有同名代理", proxy_name)

    existing = find_account_by_name(token, name)
    body = {
        "name": name,
        "platform": "openai",
        "type": "oauth",
        "credentials": creds,
        "concurrency": int(_cfg("SUB2API_CONCURRENCY", 10) or 10),
        "priority": int(_cfg("SUB2API_PRIORITY", 1) or 1),
    }

    if existing:
        acc_id = int(existing.get("id"))
        _request("PUT", f"/api/v1/admin/accounts/{acc_id}",
                 token=token, payload=body)
        updated = True
    else:
        out = _request("POST", "/api/v1/admin/accounts",
                       token=token, payload=body)
        acc_id = int((out.get("data") or {}).get("id") or 0)
        updated = False
        if not acc_id:
            raise Sub2apiError("创建成功但没拿到账号 id")

    # 分组和代理必须单独 PUT：创建接口收下 group_ids 但不会生效
    patch = {"group_ids": groups}
    if proxy_id:
        patch["proxy_id"] = proxy_id
    _request("PUT", f"/api/v1/admin/accounts/{acc_id}",
             token=token, payload=patch)

    return {
        "name": name,
        "account_id": acc_id,
        "proxy_id": proxy_id,
        "proxy_name": proxy_name if proxy_id else "",
        "group_ids": groups,
        "updated": updated,
    }
