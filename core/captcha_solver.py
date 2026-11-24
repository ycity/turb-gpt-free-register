# -*- coding: utf-8 -*-
"""2Captcha 打码客户端。

为什么需要它：Stripe 在「加卡」和「提交支付」两步都会弹 hCaptcha
Enterprise（sitekey 形如 c7faac4c-...），并且带 rqdata——那是 Stripe 把
验证码和当次 SetupIntent/PaymentIntent 绑在一起的签名。自动流程过不了它
就永远停在 awaiting_human。

设计要点：
  - rqdata 是每次会话现生成的，只能"先抓参数、再提交、再把 token 注入回
    同一个浏览器"，不能预先批量解。
  - 2Captcha 的 in.php 同步接受任务、res.php 轮询结果，token 有效期约
    2 分钟，所以要马上注入。
  - 失败要快速返回而不是把整个支付流程拖死：超时就当没解出来，调用方
    仍可回落到"等人点"。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

API_IN = "https://2captcha.com/in.php"
API_RES = "https://2captcha.com/res.php"

#: 2Captcha 的 hCaptcha 任务在「无可用工人」时会返回这个，值得重试。
_RETRYABLE = ("ERROR_NO_SLOT_AVAILABLE", "ERROR_SERVER_OVERLOAD")


class CaptchaError(RuntimeError):
    """打码失败。"""


def _post(url: str, data: dict[str, str], timeout: float) -> str:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _get(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _parse_in_response(raw: str) -> tuple[bool, str]:
    """解析 in.php 的响应，返回 (是否受理, task_id 或错误串)。

    2Captcha 两种格式都要认：
      - 带 json=1：{"status":1,"request":"84454168220"}
      - 不带：       OK|84454168220
    只认 OK| 前缀会把握手成功当失败（实测 2026-11-18：json=1 时返回
    {"status":1,...}，被误判成"任务被拒"，白等一轮）。
    """
    text = (raw or "").strip()
    if not text:
        return False, ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except Exception:
            return False, text
        status = data.get("status")
        req = str(data.get("request") or "")
        if status == 1 and req:
            return True, req
        return False, req or text
    if text.startswith("OK|"):
        return True, text.split("|", 1)[1].strip()
    return False, text


def _parse_res_response(raw: str) -> tuple[str, str]:
    """解析 res.php 的响应，返回 (状态, 值)。

    状态：'ready'（拿到了 token）/ 'waiting'（还没好）/ 'error'。
    request 为 "CAPCHA_NOT_READY"（2Captcha 原文拼写如此）时继续等。
    """
    text = (raw or "").strip()
    if not text:
        return "error", ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except Exception:
            return "error", text
        status = data.get("status")
        req = str(data.get("request") or "")
        if status == 1 and req:
            return "ready", req
        if req.upper() in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
            return "waiting", req
        return "error", req or text
    if text.startswith("OK|"):
        return "ready", text.split("|", 1)[1].strip()
    if text.strip().upper() in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
        return "waiting", text
    return "error", text


def solve_hcaptcha(
    *,
    api_key: str,
    sitekey: str,
    pageurl: str,
    user_agent: str = "",
    rqdata: str = "",
    enterprise: bool = True,
    invisible: bool = False,
    timeout: float = 180.0,
    poll_interval: float = 5.0,
    submit_timeout: float = 60.0,
) -> str:
    """解一次 hCaptcha，返回可注入页面的 token。

    api_key 为空或参数不全时抛 CaptchaError，由调用方决定是否回落人工。
    """
    api_key = str(api_key or "").strip()
    sitekey = str(sitekey or "").strip()
    pageurl = str(pageurl or "").strip()
    if not api_key:
        raise CaptchaError("未配置 2Captcha API Key")
    if not sitekey:
        raise CaptchaError("缺少 sitekey")
    if not pageurl:
        raise CaptchaError("缺少 pageurl")

    payload = {
        "key": api_key,
        "method": "hcaptcha",
        "sitekey": sitekey,
        "pageurl": pageurl,
        "json": "1",
    }
    if enterprise:
        # Enterprise 模式必须显式声明，否则 2Captcha 按普通 hCaptcha 解，
        # 拿回来的 token 在 Stripe 侧校验不过。
        payload["enterprise"] = "1"
    if invisible:
        payload["invisible"] = "1"
    if user_agent:
        # 必须与真正提交页面时浏览器的 UA 一致，否则 token 与指纹不匹配
        payload["userAgent"] = user_agent
    if rqdata:
        payload["rqdata"] = rqdata

    logger.info("[打码] 提交 hCaptcha Enterprise sitekey=%s rqdata=%s ua=%s",
                sitekey[:12], "有" if rqdata else "无",
                "有" if user_agent else "无")

    cid = ""
    for attempt in range(3):
        try:
            raw = _post(API_IN, payload, submit_timeout)
        except Exception as exc:  # noqa: BLE001
            raise CaptchaError(f"提交打码任务失败：{type(exc).__name__}: {exc}") from exc
        ok, cid_or_err = _parse_in_response(raw)
        if ok and cid_or_err:
            cid = cid_or_err
            break
        if any(k in raw for k in _RETRYABLE) and attempt < 2:
            logger.warning("[打码] 无可用工人，%ss 后重试：%s", poll_interval * 2, raw[:80])
            time.sleep(poll_interval * 2)
            continue
        raise CaptchaError(f"打码任务被拒：{raw[:160]}")
    if not cid:
        raise CaptchaError("打码任务没有返回 id")

    logger.info("[打码] 任务已受理 id=%s，开始轮询", cid)
    deadline = time.time() + max(30.0, float(timeout))
    while time.time() < deadline:
        time.sleep(max(2.0, float(poll_interval)))
        q = urllib.parse.urlencode(
            {"key": api_key, "action": "get", "id": cid, "json": "1"})
        try:
            raw = _get(f"{API_RES}?{q}", submit_timeout)
        except Exception as exc:  # noqa: BLE001 - 轮询抖动不该中断
            logger.debug("[打码] 轮询异常：%s", exc)
            continue
        state, value = _parse_res_response(raw)
        if state == "ready" and value:
            logger.info("[打码] 解出 token（%s 字符）", len(value))
            return value
        if state == "waiting":
            continue
        raise CaptchaError(f"打码失败：{raw[:160]}")
    raise CaptchaError(f"打码超时（>{int(timeout)}s）")
