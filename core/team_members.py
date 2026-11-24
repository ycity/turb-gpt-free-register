# -*- coding: utf-8 -*-
"""ChatGPT Team 成员管理。

接口口径来自真实抓包（chatgpt.com/admin/members 页面）：
    GET    /backend-api/accounts/{ws}/users?offset=&limit=&query=
    GET    /backend-api/accounts/{ws}/invites?offset=&limit=&query=
    GET    /backend-api/accounts/{ws}/users/seat_type_counts
    POST   /backend-api/accounts/{ws}/invites
    DELETE /backend-api/accounts/{ws}/invites          body {email_address}
    DELETE /backend-api/accounts/{ws}/users/{user_id}

两个必须说清的点：

1. workspace id 用 paid_workspace_account_id，不是 token 里的 account_id。
   Team 成员的 token 往往指向个人 free workspace，团队挂在另一个上，
   用错会操作到错误的 workspace 或直接 404。

2. 席位总数不在任何一个"看起来像"的接口里。seat_type_counts 返回的
   maximum_seats 是平台上限（200），不是买了几个；真实购买数要从
   发票行的 quantity 读。所以这里两处都取，以发票为准。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = "https://chatgpt.com"
REFERER_MEMBERS = "https://chatgpt.com/admin/members"

# seat_type：default = ChatGPT 席位，usage_based = Codex 席位
SEAT_DEFAULT = "default"
SEAT_CODEX = "usage_based"

ROLES = ("standard-user", "account-admin", "account-owner", "analytics-viewer")


class TeamMemberError(RuntimeError):
    """成员管理操作失败。"""


def _ws_of(account: dict) -> str:
    """取付费 workspace id。"""
    from core.chatgpt_plan import normalize_token, token_claims

    ws = str(account.get("paid_workspace_account_id") or "").strip()
    if ws:
        return ws
    claims = token_claims(normalize_token(str(account.get("access_token") or "")))
    return str(claims.get("account_id") or "").strip()


class _Client:
    """一次性会话；用完必须 close()。"""

    def __init__(self, account: dict, *, timeout: float = 45.0):
        from core.chatgpt_plan import (
            normalize_token, open_plan_check_proxy, resolve_plan_check_route,
            token_claims, _warm_plan_session,
        )
        from core.session import BrowserSession

        self.token = normalize_token(str(account.get("access_token") or ""))
        if not self.token:
            raise TeamMemberError("该账号没有 access_token")
        self.claims = token_claims(self.token)
        if self.claims.get("token_expired") is True:
            raise TeamMemberError("access_token 已过期，请先查活刷新")
        self.ws = _ws_of(account)
        if not self.ws:
            raise TeamMemberError("拿不到 workspace id，请先查一次套餐")

        route = resolve_plan_check_route(str(account.get("proxy_used") or ""))
        self._eff, self._relay = open_plan_check_proxy(
            route, route["proxy"], timeout=timeout)
        self.env = BrowserSession(
            proxy=self._eff, detect_exit_geo=True,
            fingerprint_seed=f"team-members:{uuid.uuid4()}")
        _warm_plan_session(self.env)

    def close(self) -> None:
        try:
            self.env.close()
        except Exception:  # noqa: BLE001 - 清理失败不该掩盖业务结果
            logger.debug("关闭会话失败", exc_info=True)
        if self._relay is not None:
            try:
                self._relay.stop()
            except Exception:  # noqa: BLE001
                logger.debug("停止代理中继失败", exc_info=True)

    def _headers(self, path: str, *, json_body: bool = False) -> dict:
        from core.chatgpt_plan import _common_headers

        h = _common_headers(self.env, self.token, self.claims)
        base = path.split("?")[0]
        h["x-openai-target-path"] = base
        h["x-openai-target-route"] = base
        # 必须显式覆盖：_common_headers 填的是 token 里的 account_id，
        # 而我们要操作的是付费 workspace。
        h["chatgpt-account-id"] = self.ws
        h["referer"] = REFERER_MEMBERS
        if json_body:
            h["content-type"] = "application/json"
        return h

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = API_BASE + path
        headers = self._headers(path, json_body=body is not None)
        try:
            if method == "GET":
                resp = self.env.get(url, headers=headers, allow_redirects=False)
            elif method == "POST":
                resp = self.env.post(url, headers=headers,
                                     data=json.dumps(body or {}),
                                     allow_redirects=False)
            elif method == "DELETE":
                resp = self.env.delete(url, headers=headers,
                                       data=json.dumps(body or {}),
                                       allow_redirects=False)
            else:
                raise TeamMemberError(f"不支持的方法：{method}")
        except Exception as exc:  # noqa: BLE001
            raise TeamMemberError(f"{type(exc).__name__}: {exc}") from exc

        if resp.status_code >= 400:
            detail = (resp.text or "")[:300]
            raise TeamMemberError(f"HTTP {resp.status_code}: {detail}")
        try:
            return resp.json()
        except Exception:  # noqa: BLE001 - 删除成功时可能返回空体
            return {}


def _seat_label(seat: str) -> str:
    return {SEAT_DEFAULT: "ChatGPT", SEAT_CODEX: "Codex"}.get(str(seat or ""),
                                                              str(seat or ""))


def fetch_overview(account: dict) -> dict:
    """一次拿齐成员、待处理邀请、席位占用。

    面板打开时调。席位数必须和成员列表一起给，否则人没法判断还能不能邀。
    """
    c = _Client(account)
    try:
        ws = c.ws
        users = c.request(
            "GET", f"/backend-api/accounts/{ws}/users?offset=0&limit=100&query=")
        invites = c.request(
            "GET", f"/backend-api/accounts/{ws}/invites?offset=0&limit=100&query=")
        try:
            counts = c.request(
                "GET", f"/backend-api/accounts/{ws}/users/seat_type_counts")
        except TeamMemberError:
            counts = {}

        total_seats = _purchased_seats(c, ws)

        members = [{
            "user_id": str(u.get("id") or ""),
            "email": str(u.get("email") or ""),
            "name": str(u.get("name") or ""),
            "role": str(u.get("role") or ""),
            "seat_type": str(u.get("seat_type") or ""),
            "seat_label": _seat_label(u.get("seat_type")),
            "created_time": u.get("created_time") or "",
            "is_owner": str(u.get("role") or "") == "account-owner",
        } for u in (users.get("items") or [])]

        pending = [{
            "invite_id": str(i.get("id") or ""),
            "email": str(i.get("email_address") or ""),
            "role": str(i.get("role") or ""),
            "seat_type": str(i.get("seat_type") or ""),
            "seat_label": _seat_label(i.get("seat_type")),
            "created_time": i.get("created_time") or "",
        } for i in (invites.get("items") or [])]

        seat_counts = (counts.get("seat_type_counts") or {})
        used = len(members) + len(pending)
        return {
            "ok": True,
            "workspace_id": ws,
            "members": members,
            "pending_invites": pending,
            "member_count": len(members),
            "pending_count": len(pending),
            "seat_counts": seat_counts,
            "total_seats": total_seats,
            "used_seats": used,
            # 待处理邀请也占位：对方一接受就占掉席位，不扣掉会超发
            "free_seats": (max(0, total_seats - used) if total_seats else None),
        }
    finally:
        c.close()


def _purchased_seats(c: _Client, ws: str) -> int:
    """买了几个席位。

    seat_type_counts 的 maximum_seats 是平台上限（200），不是购买数，
    拿它当席位总数会让"还剩几个"完全失真。购买数只能从发票行读。
    """
    try:
        data = c.request("GET", f"/backend-api/invoices?limit=1&account_id={ws}")
    except TeamMemberError:
        return 0
    for inv in (data.get("data") or []):
        for line in ((inv.get("lines") or {}).get("data") or []):
            qty = line.get("quantity")
            try:
                n = int(qty)
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
    return 0


def invite_members(account: dict, emails: list[str], *,
                   role: str = "standard-user",
                   seat_type: str = SEAT_DEFAULT,
                   allow_overflow: bool = False) -> dict:
    """邀请成员。

    默认会拦住超出席位的邀请：席位买了几个就只能用几个，超发的邀请
    对方点了也进不来，白白消耗一次。allow_overflow=True 可强制发。
    """
    emails = [str(e or "").strip() for e in (emails or [])]
    emails = [e for e in emails if e]
    if not emails:
        raise TeamMemberError("没有要邀请的邮箱")
    if role not in ROLES:
        raise TeamMemberError(f"不支持的角色：{role}")
    if seat_type not in (SEAT_DEFAULT, SEAT_CODEX):
        raise TeamMemberError(f"不支持的席位类型：{seat_type}")

    c = _Client(account)
    try:
        ws = c.ws
        if not allow_overflow:
            total = _purchased_seats(c, ws)
            if total:
                users = c.request(
                    "GET",
                    f"/backend-api/accounts/{ws}/users?offset=0&limit=100&query=")
                invites = c.request(
                    "GET",
                    f"/backend-api/accounts/{ws}/invites?offset=0&limit=100&query=")
                used = len(users.get("items") or []) + len(invites.get("items") or [])
                free = total - used
                if len(emails) > free:
                    raise TeamMemberError(
                        f"席位不够：共 {total} 个，已用 {used} 个"
                        f"（含待处理邀请），只剩 {max(0, free)} 个，"
                        f"这次要邀 {len(emails)} 个")

        out = c.request("POST", f"/backend-api/accounts/{ws}/invites", {
            "email_addresses": emails,
            "role": role,
            "seat_type": seat_type,
            "resend_emails": True,
        })
        invited = [str((x or {}).get("email_address") or "")
                   for x in (out.get("account_invites") or [])]
        errored = out.get("errored_emails") or []
        already = out.get("already_member_emails") or []
        logger.info("[Team成员] 邀请 ws=%s 成功=%s 失败=%s 已是成员=%s",
                    ws, invited, errored, already)
        return {
            "ok": not errored,
            "invited": invited,
            "errored_emails": errored,
            "already_member_emails": already,
        }
    finally:
        c.close()


def revoke_invite(account: dict, email: str) -> dict:
    """撤销一个待处理邀请，把席位放回去。"""
    email = str(email or "").strip()
    if not email:
        raise TeamMemberError("缺少邮箱")
    c = _Client(account)
    try:
        c.request("DELETE", f"/backend-api/accounts/{c.ws}/invites",
                  {"email_address": email})
        logger.info("[Team成员] 已撤销邀请 ws=%s %s", c.ws, email)
        return {"ok": True, "email": email}
    finally:
        c.close()


def remove_member(account: dict, user_id: str) -> dict:
    """移除已加入的成员，把席位放回去。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        raise TeamMemberError("缺少 user_id")
    c = _Client(account)
    try:
        c.request("DELETE", f"/backend-api/accounts/{c.ws}/users/{user_id}")
        logger.info("[Team成员] 已移除成员 ws=%s %s", c.ws, user_id)
        return {"ok": True, "user_id": user_id}
    finally:
        c.close()
