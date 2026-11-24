# -*- coding: utf-8 -*-
"""akmoz（闪取）邮箱取码客户端。

素材格式（粘贴整段文本即可导入）：
    邮箱---邮箱密码---接码链接（直接验证码登录openai）：xxx@hotmail.com----pwd----https://email.akmoz.com/email_query?email=xxx%40hotmail.com

取码接口（从页面 JS 里挖出来的真实接口，不是那个 HTML 页面）：
    POST https://email.akmoz.com/api/email/fetch_mails
    {"email": "xxx@hotmail.com"}
    -> {"channel": "REST V2", "inbox": [{fullBody, bodyPreview, from, subject,
                                         receivedDateTime}, ...]}

注意 email_query 那个链接返回的是前端页面（5 万字节 HTML），
直接拿它取码会解析不出东西——必须用上面的 API。
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_API_HOST = "https://email.akmoz.com"
_FETCH_PATH = "/api/email/fetch_mails"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 进程内缓存：注册流程里领到邮箱后，后续取码不用再查库
_CONTEXT_CACHE: dict[str, "AkmozEmailAccount"] = {}


class AkmozMailError(RuntimeError):
    """akmoz 邮箱相关错误。"""


@dataclass
class AkmozEmailAccount:
    email: str
    password: str = ""
    code_url: str = ""


def _cfg(name: str, default=None):
    try:
        from config import email as _email_cfg
        return getattr(_email_cfg, name, default)
    except Exception:  # noqa: BLE001
        return default


def api_host_of(code_url: str) -> str:
    """从取码链接里取出服务域名，没有就用默认。

    同一套系统可能换域名部署，链接里带的才是权威值。
    """
    raw = str(code_url or "").strip()
    if not raw:
        return _API_HOST
    try:
        p = urllib.parse.urlparse(raw)
        if p.scheme and p.netloc:
            return "%s://%s" % (p.scheme, p.netloc)
    except Exception:  # noqa: BLE001
        pass
    return _API_HOST


def fetch_inbox(email: str, code_url: str = "", *,
                timeout: float = 25.0) -> list[dict]:
    """拉这个邮箱的收件箱。失败抛 AkmozMailError。"""
    email = str(email or "").strip()
    if not email:
        raise AkmozMailError("邮箱为空")
    url = api_host_of(code_url) + _FETCH_PATH
    body = json.dumps({"email": email}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", _UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Referer", api_host_of(code_url) + "/email_query")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise AkmozMailError(
            "取信失败：%s: %s" % (type(exc).__name__, str(exc)[:120])) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AkmozMailError("取信返回的不是 JSON：%s" % raw[:120]) from exc
    inbox = data.get("inbox") if isinstance(data, dict) else None
    return list(inbox or [])


def _msg_ts(msg: dict) -> float:
    """邮件时间戳。解析不出来返回 0，让调用方当成"时间未知"处理。"""
    raw = str(msg.get("receivedDateTime") or msg.get("date")
              or msg.get("time") or "")
    if not raw:
        return 0.0
    txt = raw.replace("Z", "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(txt[:19], fmt)
            return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def _msg_text(msg: dict) -> str:
    return "%s\n%s\n%s" % (
        msg.get("subject") or "",
        msg.get("fullBody") or "",
        msg.get("bodyPreview") or "")


def _is_openai(msg: dict) -> bool:
    frm = str(((msg.get("from") or {}).get("emailAddress") or {}
               ).get("address", "")).lower()
    if "openai" in frm:
        return True
    # 有些转发邮件发件人不是 openai，但正文里会提到
    return "openai" in _msg_text(msg).lower()


def fetch_latest_otp(email: str, after_ts: float | None = None,
                     max_wait: int | None = None,
                     poll_interval: int | None = None,
                     settle_seconds: int | None = None,
                     **_ignored) -> str:
    """等这个邮箱最新的 OpenAI 验证码。

    只认 after_ts 之后到的 OpenAI 邮件——否则会把上一次注册留下的旧码
    当成新的，填进去必然失败。时间解析不出来的邮件不做时间过滤，
    宁可多看一封也不要漏掉（akmoz 的时间字段格式曾经变过）。
    """
    from core.generic_api_mail_client import _extract_code

    account = get_account_context(email)
    code_url = account.code_url if account else ""

    max_wait = int(max_wait if max_wait is not None
                   else (_cfg("OTP_MAX_WAIT", 90) or 90))
    poll = int(poll_interval if poll_interval is not None
               else (_cfg("OTP_POLL_INTERVAL", 3) or 3))
    # 时间戳留一点回看窗口：akmoz 的时间是服务端的，和本地可能有偏差
    floor = (float(after_ts) - 180.0) if after_ts else 0.0

    deadline = time.time() + max_wait
    last_err = ""
    while time.time() < deadline:
        try:
            inbox = fetch_inbox(email, code_url)
        except AkmozMailError as exc:
            last_err = str(exc)
            logger.debug("[akmoz] %s", last_err)
            time.sleep(poll)
            continue

        # 新邮件在前
        inbox.sort(key=_msg_ts, reverse=True)
        for msg in inbox:
            if not _is_openai(msg):
                continue
            ts = _msg_ts(msg)
            if floor and ts and ts < floor:
                continue
            code = _extract_code(_msg_text(msg))
            if code:
                logger.info("[akmoz] %s 取到验证码 %s", email, code)
                return code
        time.sleep(poll)

    raise AkmozMailError(
        "等待 %s 的 OTP 超时（>%ds）%s" % (
            email, max_wait, ("；最后一次错误：" + last_err) if last_err else ""))


def pick_account() -> AkmozEmailAccount:
    """从邮箱池领一个可用的 akmoz 邮箱。"""
    from core.db import akmoz_email_pool_summary, claim_next_akmoz_email

    row = claim_next_akmoz_email()
    if row is None:
        raise AkmozMailError(
            "akmoz 邮箱池没有可用账号：%s。请在 WebUI 邮箱池导入素材行"
            % akmoz_email_pool_summary())
    acc = AkmozEmailAccount(email=row["email"],
                            password=str(row.get("password") or ""),
                            code_url=str(row.get("code_url") or ""))
    _CONTEXT_CACHE[acc.email.lower()] = acc
    logger.info("[akmoz] 选中邮箱: %s（DB id=%s）", acc.email, row.get("id"))
    return acc


def get_account_context(email: str) -> AkmozEmailAccount | None:
    key = str(email or "").strip().lower()
    if not key:
        return None
    if key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[key]
    from core.db import get_akmoz_email_by_email
    row = get_akmoz_email_by_email(key)
    if row is None:
        return None
    acc = AkmozEmailAccount(email=row["email"],
                            password=str(row.get("password") or ""),
                            code_url=str(row.get("code_url") or ""))
    _CONTEXT_CACHE[key] = acc
    return acc


def release_account(email: str, status: str = "available",
                    note: str | None = None) -> None:
    from core.db import release_akmoz_email
    release_akmoz_email(email, status=status, note=note)


def parse_material(text: str) -> list[dict]:
    """把粘贴的素材文本解析成记录列表。

    支持的写法（同一段文本里可以混用）：
      邮箱---邮箱密码---接码链接（...）：a@x.com----pwd----https://.../email_query?email=a%40x.com
      a@x.com----pwd----https://akmoz.com/2FA#<TOTP_SECRET>
      a@x.com----pwd----https://.../email_query?email=a%40x.com
      a@x.com----pwd
      a@x.com

    第三列有两种含义，靠内容区分：
      - 含 "2FA#"  -> 后面那串是 TOTP 密钥（账号已注册且已开 2FA），
                      这类素材登录时走 TOTP，不需要邮箱取码。
      - 其它 URL    -> 取码链接，登录/注册时用它拉邮箱 OTP。

    前缀说明文字会被丢掉（中英文冒号都认）；没给取码链接时按邮箱自动拼。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 丢掉 "邮箱---邮箱密码---接码链接（...）：" 这类前缀说明
        for sep in ("：", ":"):
            if sep in line and "@" in line.split(sep, 1)[1]:
                line = line.split(sep, 1)[1].strip()
                break
        if not line:
            continue

        # 分隔符：常见 ----（4 个），但这批素材是 ---（3 个）。
        # 先把 4 个以上的横线归一到 ---，再按 --- 切；密码里也可能有 -，
        # 所以只按"连续 3 个及以上横线"切，不能用单横线。
        norm = re.sub(r"-{3,}", "---", line).replace("====", "---")
        parts = [p.strip() for p in norm.split("---")]
        parts = [p for p in parts if p]
        if not parts:
            continue
        email = parts[0]
        if "@" not in email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)

        password = parts[1] if len(parts) > 1 else ""
        code_url = ""
        totp_secret = ""
        for p in parts[1:]:
            low = p.lower()
            if low.startswith(("http://", "https://")):
                if "2fa#" in low:
                    # TOTP 链接：密钥在 # 后面
                    totp_secret = p.split("#", 1)[1].strip()
                    continue
                if not code_url:
                    code_url = p
                    if p == password:
                        password = ""
        if not code_url:
            code_url = "%s/email_query?email=%s" % (
                _API_HOST, urllib.parse.quote(email))
        row = {"email": email, "password": password, "code_url": code_url}
        if totp_secret:
            row["totp_secret"] = totp_secret
        out.append(row)
    return out
