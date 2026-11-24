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

# 调度优先级：周限额账号额度宽松（没有 5 小时滚动限制），排在前面多用；
# 5h 账号容易触顶，优先级压低。数字越大越优先。
PRIORITY_BY_KIND = {"weekly": 8, "5h": 3}

# 账号能用哪些模型。不带这张表的话 sub2api 认不出可用模型，
# 连测试连接都会被 OpenAI 以 model_not_found 拒掉。
# 取自 sub2api 里已有账号（AG8-Team1）的 model_mapping，键值相同。
MODEL_NAMES = (
    "codex-auto-review",
    "gpt-4o-audio-preview", "gpt-4o-realtime-preview",
    "gpt-5.2", "gpt-5.2-2025-12-11", "gpt-5.2-chat-latest",
    "gpt-5.2-pro", "gpt-5.2-pro-2025-12-11",
    "gpt-5.3-codex-spark",
    "gpt-5.4", "gpt-5.4-2026-03-05", "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-6", "gpt-6-astra", "gpt-6-chat-thinking", "gpt-6-luna", "gpt-6-sol",
    "gpt-6.1-sol", "gpt-6.1-terra",
    "gpt-image-1", "gpt-image-1.5", "gpt-image-2",
    "gpt-image-2.5-flare", "gpt-image-2.5-sunburst",
    "gpt-reserve",
)


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
        "model_mapping": {m: m for m in MODEL_NAMES},
    }


def _proxy_reachable(host: str, port: int, timeout: float = 6.0) -> bool:
    """代理端口通不通。不通就别往账号上挂，否则 sub2api 调度过去必然失败。"""
    import socket
    try:
        with socket.create_connection((str(host), int(port)), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def find_proxy_id(token: str, proxy_name: str,
                  *, fallback_name: str = "") -> tuple[int, str]:
    """按名字找代理，顺带确认它还活着。

    sub2api 的代理名和我们代理池一致，所以能直接按名匹配。但注册时用的
    出口可能后来挂了（实测 Aaitr-Frontier 整台不可达），这种情况下退到
    fallback_name，免得导进去的账号一调度就失败。

    返回 (proxy_id, 实际用的名字)。
    """
    name = str(proxy_name or "").strip()
    out = _request("GET", "/api/v1/admin/proxies?page=1&page_size=200",
                   token=token)
    items = (out.get("data") or {}).get("items") or []
    by_name = {str(p.get("name") or "").strip().lower(): p for p in items}

    for want in (name, str(fallback_name or "").strip()):
        if not want:
            continue
        p = by_name.get(want.lower())
        if not p:
            continue
        if _proxy_reachable(p.get("host"), p.get("port")):
            return int(p.get("id") or 0), str(p.get("name") or "")
        logger.warning("[sub2api] 代理 %s (%s:%s) 不可达，尝试回退",
                       p.get("name"), p.get("host"), p.get("port"))
    return 0, ""


def find_account_by_name(token: str, name: str) -> dict | None:
    out = _request("GET", "/api/v1/admin/accounts?page=1&page_size=200",
                   token=token)
    items = (out.get("data") or {}).get("items") or []
    for a in items:
        if str(a.get("name") or "") == name:
            return a
    return None


def test_account(account_id: int, *, token: str = "", model: str = "",
                 timeout: float = 90.0) -> dict:
    """调 sub2api 的测试连接，返回 {ok, model, error, reply}。

    参数名是 model_id（不是 model），留空时 sub2api 用自己的默认模型
    gpt-5.4，而 Codex 账号不支持它，必然报 model_not_found。
    响应是 SSE 流：test_start / content / test_complete / error。
    """
    tok = token or login()
    want = str(model or _cfg("SUB2API_TEST_MODEL", "gpt-6-luna") or "gpt-6-luna")
    url = _base_url() + f"/api/v1/admin/accounts/{int(account_id)}/test"
    body = json.dumps({"model_id": want, "prompt": ""}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return {"ok": False, "model": want,
                "error": f"HTTP {exc.code}: {detail[:200]}"}
    except Exception as exc:
        return {"ok": False, "model": want,
                "error": f"{type(exc).__name__}: {exc}"}

    used, error, ok = want, "", False
    chunks: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].strip())
        except Exception:  # noqa: BLE001 - 非 JSON 的心跳行跳过
            continue
        kind = str(evt.get("type") or "")
        if kind == "test_start":
            used = str(evt.get("model") or want)
        elif kind == "content":
            chunks.append(str(evt.get("text") or ""))
        elif kind == "test_complete":
            ok = bool(evt.get("success"))
        elif kind == "error":
            error = str(evt.get("error") or "")
    return {
        "ok": ok and not error,
        "model": used,
        "error": error,
        "reply": "".join(chunks)[:200],
    }


def set_privacy(acc_id: int, token: str = "") -> dict:
    """调 sub2api 的「设置隐私」，关掉 ChatGPT 侧的训练数据共享。

    成功时 sub2api 把结果写进账号 extra.privacy_mode，只有 training_off
    才算真关掉；training_set_cf_blocked 表示被 Cloudflare 拦了，训练可能还开着。
    """
    tok = token or login()
    try:
        _request("POST", f"/api/v1/admin/accounts/{int(acc_id)}/set-privacy",
                 token=tok, timeout=90.0)
    except Exception as exc:  # noqa: BLE001 - 隐私没关掉不该让导入整体失败
        return {"ok": False, "mode": "", "error": f"{type(exc).__name__}: {exc}"}

    mode = ""
    try:
        out = _request("GET", f"/api/v1/admin/accounts/{int(acc_id)}", token=tok)
        extra = ((out.get("data") or {}).get("extra") or {})
        mode = str(extra.get("privacy_mode") or "")
    except Exception as exc:  # noqa: BLE001 - 回读失败只影响判定，不影响已下发的操作
        return {"ok": False, "mode": "", "error": f"回读失败: {exc}"}

    if mode == "training_off":
        return {"ok": True, "mode": mode, "error": ""}
    if mode == "training_set_cf_blocked":
        return {"ok": False, "mode": mode, "error": "被 Cloudflare 拦截，训练可能仍开启"}
    return {"ok": False, "mode": mode, "error": "关闭训练数据共享失败"}


def import_account(
    *,
    codex_payload: dict,
    subscription_no,
    subscription_kind: str,
    proxy_name: str = "",
    fallback_proxy: str = "",
    group_ids: list[int] | None = None,
    run_test: bool = True,
    disable_training: bool = True,
) -> dict:
    """导入一个账号，返回 {name, account_id, proxy, groups, updated}。

    同名账号存在时更新而不是重复建——重复建会让 sub2api 调度到失效凭据。
    """
    token = login()
    name = build_account_name(subscription_no, subscription_kind)
    creds = build_credentials(codex_payload)
    groups = list(group_ids or _cfg("SUB2API_GROUP_IDS", [6]) or [6])

    proxy_id, used_proxy = (0, "")
    if proxy_name or fallback_proxy:
        proxy_id, used_proxy = find_proxy_id(
            token, proxy_name, fallback_name=fallback_proxy)
    if proxy_name and not proxy_id:
        logger.warning("[sub2api] 没找到可用代理（想要 %s，回退 %s）",
                       proxy_name, fallback_proxy or "无")

    existing = find_account_by_name(token, name)
    body = {
        "name": name,
        "platform": "openai",
        "type": "oauth",
        "credentials": creds,
        "concurrency": int(_cfg("SUB2API_CONCURRENCY", 10) or 10),
        "priority": PRIORITY_BY_KIND.get(str(subscription_kind or ""), 3),
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

    result = {
        "name": name,
        "account_id": acc_id,
        "proxy_id": proxy_id,
        "proxy_name": used_proxy,
        "group_ids": groups,
        "priority": body["priority"],
        "updated": updated,
    }
    if disable_training:
        result["privacy"] = set_privacy(acc_id, token=token)
    if run_test:
        result["test"] = test_account(acc_id, token=token)
    return result
