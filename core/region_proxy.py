# -*- coding: utf-8 -*-
"""按地区从代理池挑出口。

用途：1024proxy 注册的号，出口是临时 IP（保留 120 分钟就失效）。后续查活
如果还用注册时保存的 proxy_used，必然连不通——而 LIVE_CHECK_SWITCH_PROXY_
ON_FAILURE 默认关着，失败后不换出口直接判失败，于是拿到的是"网络失败"
而不是账号真实死活（2026-11-18 发现）。

做法：按账号注册时记下的地区（proxy_region，形如 1024·CA），从代理池里
挑一个同地区的长期出口。池里没有该地区就回落 US——跨国总比连不通强，
而且 US 是池里最常见的地区。

地区数据来源：WebUI 代理池测试结果（proxy_pool_tests 表）。那里已经存了
每个出口的实测国家，不用每次查活都重新探一遍（16 个出口逐个探要好几分钟）。
"""
from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)

# 池里找不到同地区时的兜底国家。
# 选 US 的理由：实测池内 US 出口最多（4 个），可选面最大。
_FALLBACK_CC = "US"


def parse_region(value: str) -> str:
    """从 proxy_region 里取出国家码。

    形如 "1024·CA" -> "CA"；旧账号没有这个字段或是代理池名称时返回空。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    if "·" in s:
        s = s.split("·", 1)[1]
    s = s.strip().upper()
    # 只接受两位国家码，避免把 "Zoro-SG" 这类池名误当成地区
    return s if len(s) == 2 and s.isalpha() else ""


def pool_exits_by_country() -> dict[str, list[str]]:
    """代理池里「国家 -> 出口列表」。

    数据来自代理池测试结果，没测过的出口不计入（宁可少算，也不能靠
    名称猜国家——名称是人填的备注，和真实出口未必一致）。
    """
    out: dict[str, list[str]] = {}
    try:
        from config.proxy import PROXY_POOL, normalize_proxy_url
        from webui.proxy_pool import _load_results, proxy_key
    except Exception:  # noqa: BLE001
        logger.debug("[地区出口] 读取代理池失败", exc_info=True)
        return out

    try:
        results = _load_results()
    except Exception:  # noqa: BLE001
        logger.debug("[地区出口] 读取代理测试结果失败", exc_info=True)
        return out

    for raw in PROXY_POOL or []:
        url = normalize_proxy_url(str(raw or "").strip())
        if not url:
            continue
        try:
            res = results.get(proxy_key(url)) or {}
        except Exception:  # noqa: BLE001
            continue
        cc = str(res.get("country") or "").strip().upper()
        if len(cc) == 2 and cc.isalpha():
            out.setdefault(cc, []).append(url)
    return out


def pick_pool_exit(region: str) -> tuple[str, str]:
    """按地区挑一个代理池出口，返回 (出口, 说明)。

    同地区有就随机取一个；没有就回落 US；US 也没有就返回空串，
    让调用方走原来的逻辑（不要硬塞一个不相干的出口）。

    region 为空也会回落 US：那是 proxy_region 字段上线前注册的号，
    出口同样是临时 IP、同样早已失效，继续用它只会拿到"网络失败"。
    """
    want = parse_region(region)

    by_cc = pool_exits_by_country()
    if not by_cc:
        return "", "代理池未测出地区（请先在代理池页面测速）"

    if want:
        same = by_cc.get(want) or []
        if same:
            return random.choice(same), "同地区 %s" % want

    fb = by_cc.get(_FALLBACK_CC) or []
    if fb:
        if not want:
            return random.choice(fb), "无地区记录，用 %s" % _FALLBACK_CC
        return random.choice(fb), "池内无 %s，回落 %s" % (want, _FALLBACK_CC)

    return "", "池内既无 %s 也无 %s" % (want or "该地区", _FALLBACK_CC)


def is_ephemeral_proxy(proxy_used: str, proxy_region: str = "") -> bool:
    """这个出口是不是临时的（用完就失效）。

    1024proxy 的出口是裸 ip:port、不带认证，且账号上会记 1024· 开头的地区；
    代理池的长期出口都带 user:pass@。临时出口过期后不该再拿来查活。
    """
    if str(proxy_region or "").strip().startswith("1024"):
        return True
    p = str(proxy_used or "").strip()
    if not p:
        return False
    # 代理池出口一律带认证；不带认证的基本就是 1024proxy 现取的
    return "@" not in p


def adopt_pool_exit(account_id: int, proxy_used: str, proxy_region: str,
                    *, label: str = "") -> tuple[str, str]:
    """临时出口换成代理池出口，并把它固化成账号的注册代理。

    返回 (要用的出口, 说明)。不是临时出口时原样返回，不做任何改动。

    为什么要固化：1024proxy 的出口保留 120 分钟就失效，而查活、2FA、
    席位探测这些操作往往发生在几小时甚至几天之后。一旦某次操作改用了
    代理池出口，就把它写回 proxy_used——后续所有操作都走同一个稳定出口，
    不用每次重新挑，账号的网络指纹也保持一致（反复换出口本身就像盗号）。

    写回失败不影响本次操作：拿到出口能用就行，固化只是优化。
    """
    proxy_used = str(proxy_used or "")
    region = str(proxy_region or "")
    if not is_ephemeral_proxy(proxy_used, region):
        return proxy_used, ""

    picked, why = pick_pool_exit(region)
    if not picked:
        return proxy_used, why

    try:
        from core import db
        with db._LOCK:
            rows = db._load_accounts()
            for r in rows:
                if int(r.get("id") or 0) == int(account_id):
                    r["proxy_used"] = picked
                    # 保留地区标记（去掉 1024· 前缀），既供 is_ephemeral_proxy 识别
                    # 这已经是固化出口、不再是临时出口，也作为前端显示的兜底；
                    # 前端会优先用 proxy_used 查出这条池代理的真名（如 Zoro-SG）。
                    r["proxy_region"] = _pool_region_label(picked, region)
                    r["updated_at"] = db._now()
                    break
            db._save_accounts(rows)
        logger.info("[出口固化] 账号 #%s 的临时出口已换成代理池出口：%s（%s%s）",
                    account_id, picked.split("@")[-1], why,
                    "，触发自 %s" % label if label else "")
    except Exception:  # noqa: BLE001 - 固化失败不影响本次操作
        logger.warning("[出口固化] 写回账号 #%s 失败，本次仍用新出口",
                       account_id, exc_info=True)
    return picked, why


def _pool_region_label(proxy_url: str, old_region: str) -> str:
    """给固化后的出口起一个地区标签。

    保留原来的国家信息（它决定了账号的定价区），但去掉 1024· 前缀，
    免得 is_ephemeral_proxy 又把它当成临时出口。
    """
    cc = parse_region(old_region)
    return ("池·%s" % cc) if cc else "池"
