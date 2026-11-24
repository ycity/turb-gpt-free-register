# -*- coding: utf-8 -*-
"""注册出口：从 1024proxy 现取，随机国家，取到就验，坏了换下一个。

为什么不用固定代理池：池里那些长期出口被反复用于注册，IP 已经被 OpenAI
盯上——2026-11-17 实测 #85 之后注册的号接连被封（17 个里确认 9 个
account_deactivated，存活 0）。1024proxy 每次给新 IP，且能按分钟数申请
保留时长，更适合"一号一出口"。

为什么要"取到就验"：1024proxy 给的 IP 不保证都能用。实测 11 个地区里
KR 那个直接 TLS 握手失败（BoringSSL connect: Connection closed abruptly），
而同一批其他 10 个地区都能正常访问 chatgpt.com。坏的是**单个 IP**，不是
整体不通，所以正确的做法是换一个 IP 重取，而不是对同一个 IP 重连——
重连只会把一次失败变成三次失败。

保留时长：注册拿到号之后还要跑 2FA 绑定和单席位探测，整条链路可能十几
分钟甚至更久；代理中途失效会让后面每一步都以莫名其妙的方式失败。
1024proxy 最长支持 120 分钟，直接按上限要。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

logger = logging.getLogger(__name__)

# 验证出口是否真能用时访问的地址。用 chatgpt.com 本身而不是什么
# ip 查询服务：能连通 ip-api 不代表能连通 OpenAI，后者才是目的地。
_VERIFY_URL = "https://chatgpt.com/"


def _cfg_value(name: str, default: Any) -> Any:
    try:
        from config import register_proxy as cfg
    except Exception:  # noqa: BLE001
        return default
    return getattr(cfg, name, default)


def _candidate_regions() -> list[str]:
    """可用的国家池，已打乱。

    这些地区都实测能连通 chatgpt.com（2026-11-17）。不写死全集是因为
    1024proxy 并非每个地区都有货，取不到就跳过换下一个。
    """
    regions = list(_cfg_value("REGISTER_PROXY_REGIONS", []) or [])
    regions = [str(r).strip().upper() for r in regions if str(r).strip()]
    if not regions:
        regions = ["US", "JP", "SG", "GB", "DE", "TW", "HK", "CA", "AU"]
    random.shuffle(regions)
    return regions


def _normalize(ip_port: str) -> str:
    from config.proxy import normalize_proxy_url
    raw = str(ip_port or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://%s" % raw
    return normalize_proxy_url(raw)


_BAD_IP_KEY = "register_proxy_bad_ips"


def _host_of(proxy: str) -> str:
    """从代理地址里取出主机名（不含端口）。

    1024proxy 同一个 IP 会配不同端口反复出现，所以按主机聚合——
    换端口不换 IP 对风控没有意义。
    """
    s = str(proxy or "").strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        s = s.split("@", 1)[1]
    return s.split(":", 1)[0].strip().lower()


def bad_exit_ips() -> dict[str, int]:
    """产出过死号的出口 IP 及其死号数量。"""
    import json as _json

    from core import db
    try:
        raw = db.get_meta(_BAD_IP_KEY) or "{}"
        data = _json.loads(raw)
    except Exception:  # noqa: BLE001 - 名单坏了不该让注册停摆
        logger.warning("[注册出口] 坏 IP 名单读取失败，按空处理", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def note_bad_exit(proxy: str, *, reason: str = "") -> int:
    """记一次"这个出口产出的号死了"。返回该 IP 累计死号数。"""
    import json as _json

    from core import db
    host = _host_of(proxy)
    if not host:
        return 0
    try:
        data = bad_exit_ips()
        data[host] = data.get(host, 0) + 1
        db.set_meta(_BAD_IP_KEY, _json.dumps(data, ensure_ascii=False))
        logger.warning("[注册出口] %s 累计产出 %d 个死号%s",
                       host, data[host], ("（%s）" % reason if reason else ""))
        return data[host]
    except Exception:  # noqa: BLE001
        logger.warning("[注册出口] 记录坏 IP 失败", exc_info=True)
        return 0


def _bad_threshold() -> int:
    """死号数达到几次就拉黑这个 IP。

    默认 2：一次可能是账号自身问题或巧合，连续两次就该怀疑 IP。
    设得太高会白白烧号，太低会误杀——1024proxy 的 IP 本来就不多。
    """
    try:
        return max(1, int(_cfg_value("REGISTER_PROXY_BAD_IP_THRESHOLD", 2)))
    except (TypeError, ValueError):
        return 2


def is_bad_exit(proxy: str) -> bool:
    """这个出口是否已被拉黑。"""
    host = _host_of(proxy)
    if not host:
        return False
    return bad_exit_ips().get(host, 0) >= _bad_threshold()


def verify_exit(proxy_url: str, *, timeout: float = 20.0) -> dict:
    """确认这个出口真能访问 chatgpt.com，并报告真实出口国家。

    返回 {ok, country, detail}。不抛异常：调用方要靠返回值决定换不换。

    403 也算可用：OpenAI 对部分出口会直接返回 403，但那是业务层拦截，
    说明网络是通的；注册流程自己会处理挑战页。真正不可用的是连不上
    （TLS 失败、超时、拒绝连接）。
    """
    try:
        from core.session import BrowserSession
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "country": "", "detail": "导入会话失败: %s" % exc}

    env = None
    try:
        env = BrowserSession(proxy=proxy_url, detect_exit_geo=True,
                             fingerprint_seed="regproxy:%s" % random.random())
        country = str((env.exit_geo or {}).get("country") or "").upper()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "country": "",
                "detail": "建会话失败: %s: %s" % (type(exc).__name__, exc)}

    try:
        resp = env.get(
            _VERIFY_URL,
            headers=env.get_chatgpt_navigate_headers(
                referer=_VERIFY_URL, user_initiated=False),
            allow_redirects=True, timeout=timeout)
        code = getattr(resp, "status_code", 0)
        return {"ok": True, "country": country, "detail": "HTTP %s" % code}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "country": country,
                "detail": "%s: %s" % (type(exc).__name__, exc)}


def acquire_register_proxy(*, minutes: int = 0, attempts: int = 0,
                           verify: bool | None = None) -> dict:
    """取一个可用的注册出口。

    每一轮换一个国家、取一批 IP，逐个验；某个 IP 不通就换下一个，
    换到可用为止。全部用尽才返回失败。

    返回 {ok, proxy, country, region, detail, tried}。
    """
    minutes = int(minutes or _cfg_value("REGISTER_PROXY_MINUTES", 120))
    attempts = int(attempts or _cfg_value("REGISTER_PROXY_MAX_ATTEMPTS", 6))
    if verify is None:
        verify = bool(_cfg_value("REGISTER_PROXY_VERIFY", True))
    per_region = max(1, int(_cfg_value("REGISTER_PROXY_IPS_PER_REGION", 2)))

    from core.team_checkout_service import fetch_1024proxy_ips

    regions = _candidate_regions()
    tried: list[str] = []
    used = 0
    skipped_bad: list[str] = []

    for region in regions:
        if used >= attempts:
            break
        try:
            ips = fetch_1024proxy_ips(region, per_region, minutes=minutes) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("[注册出口] %s 取 IP 失败：%s", region, exc)
            continue

        for ip in ips:
            if used >= attempts:
                break
            proxy = _normalize(ip)
            if not proxy:
                continue

            # 拉黑过的 IP 直接跳过，不占用 attempts 配额。
            # 1024proxy 的 IP 会重复返回，产出过死号的那些再用还是死
            #（实测 128.1.12.151 连出 3 个死号）。
            if is_bad_exit(proxy):
                skipped_bad.append(_host_of(proxy))
                logger.info("[注册出口] 跳过已拉黑的出口 %s", _host_of(proxy))
                continue

            used += 1
            tried.append("%s/%s" % (region, ip))

            if not verify:
                logger.info("[注册出口] 选用 %s（%s，未验证，保留 %s 分钟）",
                            ip, region, minutes)
                return {"ok": True, "proxy": proxy, "country": region,
                        "region": region, "detail": "未验证", "tried": tried}

            res = verify_exit(proxy)
            if res.get("ok"):
                cc = res.get("country") or region
                logger.info("[注册出口] 选用 %s（请求 %s，实际出口 %s，%s，保留 %s 分钟，"
                            "试了 %s 个%s）",
                            ip, region, cc, res.get("detail"), minutes, used,
                            "，跳过黑名单 %d 个" % len(skipped_bad)
                            if skipped_bad else "")
                return {"ok": True, "proxy": proxy, "country": cc,
                        "region": region, "detail": res.get("detail"),
                        "tried": tried}

            logger.warning("[注册出口] %s(%s) 不可用：%s，换下一个",
                           ip, region, str(res.get("detail"))[:120])
            time.sleep(0.3)

    return {"ok": False, "proxy": "", "country": "", "region": "",
            "detail": "试了 %d 个出口都不可用" % len(tried), "tried": tried}
