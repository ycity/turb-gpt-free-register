# -*- coding: utf-8 -*-
"""代理池连接测试。

按注册时的真实链路（含 PROXY_POOL_UPSTREAM_PROXY 代理链）访问 chatgpt.com 的
Cloudflare trace，测量延迟并拿到 ChatGPT 侧看到的出口 IP / 地区；成功后再按这个 IP
尽力查询归属地，补充城市与运营商。分流规则可能让不同域名走不同出口，所以归属地
按 IP 查询，而不是再问一次“我的 IP”。只返回出口信息，不记录响应正文和代理凭据。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from core.proxy_utils import diagnose_proxy_endpoint, parse_proxy_url

logger = logging.getLogger(__name__)

TRACE_URL = "https://chatgpt.com/cdn-cgi/trace"
DEFAULT_TIMEOUT = 12.0
GEO_TIMEOUT = 6.0
# 按指定 IP 查询归属地；响应字段由 BrowserSession._normalize_geo_response 统一解析。
GEO_LOOKUP_URLS = (
    "https://ipinfo.io/{ip}/json",
    "https://ipwho.is/{ip}",
    "https://ipapi.co/{ip}/json/",
)

# curl 错误码 -> 面向用户的说明；未列出的错误码只展示原始信息。
_CURL_ERROR_HINTS = {
    5: "无法解析代理服务器地址",
    6: "代理端无法解析 chatgpt.com",
    7: "无法连接代理服务器，请检查地址、端口或白名单",
    28: "连接超时",
    35: "TLS 握手失败",
    52: "代理返回了空响应",
    60: "TLS 证书校验失败，代理可能在篡改 HTTPS 流量",
    97: "SOCKS5 握手失败，请检查协议、账号密码或代理是否拒绝了目标地址",
}
_SOCKS_AUTH_REJECTED = re.compile(r"User was rejected by the SOCKS5 server", re.I)


def probe_proxy(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """测试一个代理池代理，返回出口信息；网络失败不抛异常，写入 message。"""
    from curl_cffi.requests import Session
    from core.proxy_chain import open_proxy_pool_proxy

    spec = parse_proxy_url(url)
    result = {
        "ok": False,
        "latency_ms": None,
        "ip": "",
        "country": "",
        "region": "",
        "city": "",
        "org": "",
        "colo": "",
        "datacenter": "",
        "via_upstream": False,
        "message": "",
        "detail": "",
        "tested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    relay = None
    session = None
    try:
        transport, relay = open_proxy_pool_proxy(url, timeout=timeout)
        result["via_upstream"] = relay is not None
        session = Session(impersonate="chrome")
        session.proxies = {"http": transport, "https": transport}

        started = time.perf_counter()
        resp = session.get(TRACE_URL, timeout=timeout)
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            raise ConnectionError(f"chatgpt.com 返回 HTTP {resp.status_code}")
        trace = _parse_trace(resp.text)
        if not trace.get("ip"):
            raise ConnectionError("chatgpt.com 未返回出口 IP")
        result.update(ok=True, ip=trace["ip"], country=trace.get("loc", ""), colo=trace.get("colo", ""))

        geo = _lookup_geo(session, result["ip"], min(float(timeout), GEO_TIMEOUT))
        result["region"] = str(geo.get("region") or "")
        result["city"] = str(geo.get("city") or "")
        result["org"] = str(geo.get("org") or "")
        result["country"] = result["country"] or str(geo.get("country") or "")
        result["datacenter"] = _datacenter_keyword(result["org"])
        result["message"] = "连接正常"
    except Exception as exc:
        result["ok"] = False
        result["message"], result["detail"] = _describe_error(exc)
        # 端口协议填错（例如把 HTTP 端口当成 SOCKS5）时，补一次直连握手诊断。
        if (spec.scheme.startswith("socks") and not result["via_upstream"]
                and _curl_code(exc) in (28, 56, 97) and not _SOCKS_AUTH_REJECTED.search(str(exc))):
            hint = diagnose_proxy_endpoint(url, timeout=2.0)
            if hint:
                result["message"] = f"{result['message']}（{hint}）"
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        if relay is not None:
            try:
                relay.close()
            except Exception:
                logger.warning("[代理测试] 代理链中继关闭失败", exc_info=True)
    return result


def _parse_trace(text: str) -> dict:
    out = {}
    for line in str(text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _lookup_geo(session, ip: str, timeout: float) -> dict:
    """尽力查询指定 IP 的归属地；全部失败返回空 dict，不影响连通性结论。"""
    from core.session import BrowserSession

    for template in GEO_LOOKUP_URLS:
        endpoint = template.format(ip=ip)
        try:
            resp = session.get(endpoint, headers={"Accept": "application/json"}, timeout=timeout)
            if resp.status_code != 200:
                continue
            geo = BrowserSession._normalize_geo_response(resp.json())
            if geo.get("country") or geo.get("city") or geo.get("org"):
                return geo
        except Exception as exc:
            logger.debug("[代理测试] 归属地查询失败 endpoint=%s: %s", template, exc)
    return {}


def _datacenter_keyword(org: str) -> str:
    """复用 REJECT_CLOUD_PROXY 的关键词判断运营商是否像云厂商/机房。"""
    from config import browser as browser_cfg

    text = str(org or "").lower()
    if not text:
        return ""
    keywords = getattr(browser_cfg, "CLOUD_PROXY_ORG_KEYWORDS", []) or []
    return next((str(kw) for kw in keywords if kw and str(kw).lower() in text), "")


def _curl_code(exc: BaseException) -> int | None:
    try:
        code = getattr(exc, "code", None)
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _describe_error(exc: BaseException) -> tuple[str, str]:
    """返回 (给用户看的原因, 原始错误信息)；识别不出原因时原始信息就是原因。"""
    text = " ".join(str(exc).split())
    text = re.sub(r"\s*See https://curl\.se/\S+ first for more details\.?", "", text)
    text = re.sub(r"^Failed to perform, ", "", text)
    text = re.sub(r"\.{2,}$", ".", text)
    # 代理 URL 可能出现在异常文本中，不回显账号密码。
    text = re.sub(r"(//)[^/@\s]+@", r"\1***@", text)
    code = _curl_code(exc)
    tunnel = re.search(r"CONNECT tunnel failed, response (\d+)", text)
    if tunnel and tunnel.group(1) == "407":
        hint = "代理认证失败（407），请检查用户名和密码"
    elif tunnel:
        hint = f"代理拒绝建立到 chatgpt.com 的隧道（HTTP {tunnel.group(1)}）"
    elif _SOCKS_AUTH_REJECTED.search(text):
        hint = "SOCKS5 账号密码被拒绝，请检查用户名和密码"
    elif code == 56:
        hint = "代理中断了连接"
    else:
        hint = _CURL_ERROR_HINTS.get(code, "")
    if len(text) > 200:
        text = text[:200] + "…"
    if not text:
        text = type(exc).__name__
    return (hint, text) if hint else (text, "")
