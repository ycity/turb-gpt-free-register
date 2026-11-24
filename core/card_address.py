# -*- coding: utf-8 -*-
"""按卡号取该卡登记的账单地址。

VCC 每张卡绑定的账单地址不同，而配置里只能写死一份。地址和卡对不上时
Stripe 会直接拒付（实测过 "Billing country must match request country"
这类错误），所以必须按卡取。

接口：GET https://ssr.im/address?id={完整卡号}
返回：{full_name, street, city, state, state_name, zip_code, country}

姓名也用接口返回的 full_name：它和地址同源，彼此一致。VCC 后台的
holderName 可能是另一个名字（实测 0363 卡：接口给 Clay Foy，
VCC 给 Sam Phoenix），混用会让姓名和地址对不上。
"""
from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

_API = "https://ssr.im/address?id=%s"
_TIMEOUT = 20.0
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 城市和州对不上的已知脏数据。
#
# Stripe 按真实辖区算税，不看你填的州：1588 卡的地址是
# "Philadelphia, DE"，而费城在宾州（PA，6% 州税 + 2% 市税），
# 于是 920 的八折单被收成 993.60，撞上金额闸门。
#
# 这类地址不能用来支付。在取地址时就识别出来，比每次跑到付款页
# 才被闸门拦下省事得多（一轮要几分钟，还白占一个长链）。
#
# key 是城市名小写，value 是该城市真正所属的州。
_CITY_REAL_STATE = {
    "philadelphia": "PA",
    "pittsburgh": "PA",
    "new york": "NY",
    "los angeles": "CA",
    "chicago": "IL",
    "houston": "TX",
    "seattle": "WA",
    "boston": "MA",
    "denver": "CO",
    "miami": "FL",
    "atlanta": "GA",
}


class CardAddressError(RuntimeError):
    """取不到卡对应的账单地址。"""


def fetch_card_address(card_number: str, *, timeout: float = _TIMEOUT) -> dict:
    """按完整卡号取账单地址。

    返回 {name, country, state, city, line1, line2, postal}，
    直接就是 vcc_pay_session 里 billing 字典的格式。

    取不到或字段不全时抛 CardAddressError：宁可停下，也不能拿一份
    不属于这张卡的地址去填表——那必然被拒付，还白耗一个长链。
    """
    digits = "".join(ch for ch in str(card_number or "") if ch.isdigit())
    if len(digits) < 12:
        raise CardAddressError("卡号不合法，无法查询账单地址")

    req = urllib.request.Request(_API % digits, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise CardAddressError(
            "查询账单地址失败：%s: %s" % (type(exc).__name__, exc)) from exc

    try:
        data = json.loads(body)
    except ValueError as exc:
        raise CardAddressError("账单地址接口返回的不是 JSON：%s" % body[:120]) from exc
    if not isinstance(data, dict):
        raise CardAddressError("账单地址接口返回格式非预期：%s" % body[:120])

    out = {
        "name": str(data.get("full_name") or "").strip(),
        "country": str(data.get("country") or "").strip().upper(),
        # state 用两位码（MT），不是全称（Montana）：Stripe 的州下拉按
        # 两位码匹配，填全称会选不中
        "state": str(data.get("state") or "").strip(),
        "city": str(data.get("city") or "").strip(),
        "line1": str(data.get("street") or "").strip(),
        "line2": "",
        "postal": str(data.get("zip_code") or "").strip(),
    }

    missing = [k for k in ("name", "country", "city", "line1", "postal")
               if not out[k]]
    if missing:
        raise CardAddressError(
            "账单地址缺少必填项：%s（接口返回 %s）" % ("/".join(missing), body[:160]))

    # 城市和州对不上时标出来：Stripe 按真实辖区算税，会多收一笔，
    # 结果撞上金额闸门。调用方据此换一张卡，别跑到付款页才发现。
    real = _CITY_REAL_STATE.get(out["city"].strip().lower())
    out["state_mismatch"] = bool(real and out["state"] and real != out["state"])
    if out["state_mismatch"]:
        logger.warning("[卡地址] 卡尾号 %s 的地址城市/州不匹配："
                       "%s 实际属于 %s，接口却给 %s —— 该卡会被按 %s 收税",
                       digits[-4:], out["city"], real, out["state"], real)

    logger.info("[卡地址] 卡尾号 %s -> %s, %s %s %s",
                digits[-4:], out["city"], out["state"], out["postal"],
                out["country"])
    return out
