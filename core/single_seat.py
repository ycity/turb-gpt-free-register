# -*- coding: utf-8 -*-
"""探测账号是否被服务端允许只买 1 个 Team 席位。

背景：Team 订阅默认最少 2 席。但实测发现服务端对部分账号放开了 1 席，
账号池里约有两成命中。能用 1 席就省掉一半订阅费。

判定方式（口径来自真实抓包 + 实测验证）：
    1. POST /backend-api/payments/checkout         创建一个 2 席 session
    2. GET  /backend-api/payments/checkout/openai_llc/{id}?supports_business_seat_offer=true
       读 business_minimum_seats
    3. 等于 1 就是支持单席位

三个必须说清的坑：

1. business_minimum_seats 只在带齐 CODEX_HEADERS 这 5 个头时才返回。
   少任何一个，这个字段在响应里根本不出现（不是返回 None，是整个键没有），
   会被误判成"读不到"。实测验证过。

2. 它不在创建响应里，只在 GET 详情里。创建响应只有 business_maximum_seats（200，
   那是平台上限，不是购买数）。

3. 这个值按账号固定，不是随机抽奖。同一账号连测多次结果一致：
   min=2 的账号测 13 次全是 2，min=1 的账号测 3 次全是 1。
   所以网上那种"同一账号循环 20 次碰运气"的脚本方向是错的，
   该换账号重试，而不是同账号重试。

账单国家必须和出口国家一致，否则创建直接 400 Billing country must match
request country，所以这里按出口 IP 的国家取国家和货币。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Stripe 零小数币种（minorUnitsAmount 直接等于面额，不除以 100）
_ZERO_DECIMAL_CURRENCIES = {
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA",
    "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}

CHECKOUT_CREATE = "https://chatgpt.com/backend-api/payments/checkout"
CHECKOUT_DETAIL = "https://chatgpt.com/backend-api/payments/checkout/openai_llc/{sid}"
CHECKOUT_UPDATE = "https://chatgpt.com/backend-api/payments/checkout/update"

# 缺任何一个，business_minimum_seats 字段就不返回。实测过，别精简。
CODEX_HEADERS = {
    "originator": "Codex Browser",
    "x-openai-web-frontend": "codex_webview",
    "x-openai-codex-window-type": "browser",
    "x-openai-rate-limit-join-workspace": "1",
    "x-openai-checkout-source": "chatgpt_web_merged",
}


def checkout_headers(env: Any, token: str, claims: dict | None = None) -> dict:
    """结账相关请求的统一请求头。"""
    h = env.get_chatgpt_headers(referer="https://chatgpt.com/")
    h["authorization"] = f"Bearer {token}"
    account_id = str((claims or {}).get("account_id") or "").strip()
    if account_id:
        h["chatgpt-account-id"] = account_id
    h.update(CODEX_HEADERS)
    h["content-type"] = "application/json"
    return h


def build_create_payload(workspace: str, seats: int, country: str,
                         currency: str, promo: str = "",
                         attempt_id: str = "", ui_mode: str = "custom") -> dict:
    """创建 checkout session 的请求体。

    seat_quantities 把席位拆成 default（ChatGPT）和 prolite 两类。
    不传这个字段也能建，但传了才和前端口径一致。

    ui_mode 必须是 custom：hosted 模式下服务端不返回 business_minimum_seats
    （实测过，字段整个不出现），而且 hosted 对任何账号都拒绝 1 席。
    """
    attempt_id = attempt_id or str(uuid.uuid4())
    payload = {
        "entry_point": "team_workspace_purchase_modal",
        "plan_name": "chatgptteamplan",
        "team_plan_data": {
            "workspace_name": workspace,
            "price_interval": "month",
            "seat_quantity": seats,
            "seat_quantities": [
                {"seat_type": "default", "quantity": seats},
                {"seat_type": "prolite", "quantity": 0},
            ],
        },
        "billing_details": {"country": country, "currency": currency},
        "supports_saved_payment_link": True,
        "checkout_ui_mode": ui_mode,
        "checkout_source": "chatgpt_web_merged",
        "cancel_url": "https://chatgpt.com/#pricing",
        "purchase_attempt_id": attempt_id,
    }
    if promo:
        payload["promo_code"] = promo
    return payload


def _extract_minor_amount(box: Any, currency: str = "") -> int | None:
    """从 {minorUnitsAmount, amount} 对象提取最小货币单位整数；读不到返回 None。

    严禁把缺失/None 当 0，否则接口缺字段时会被误标为"首月免费"。
    """
    if isinstance(box, bool) or box is None:
        return None
    scale = 1 if str(currency or "").strip().upper() in _ZERO_DECIMAL_CURRENCIES else 100
    if isinstance(box, (int, float)):
        return int(round(float(box)))
    if isinstance(box, dict):
        minor = box.get("minorUnitsAmount")
        if minor is not None and not isinstance(minor, bool):
            try:
                return int(round(float(minor)))
            except (TypeError, ValueError):
                pass
        amt = box.get("amount")
        if amt is not None and not isinstance(amt, bool):
            if isinstance(amt, (int, float)):
                return int(round(float(amt) * scale))
            text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", str(amt).strip())
            m = re.search(r"-?\d+(?:[.,]\d+)?", text)
            if m:
                try:
                    return int(round(float(m.group(0).replace(",", ".")) * scale))
                except ValueError:
                    pass
    return None


def _minor_to_major(minor: int | None, currency: str = "") -> float | None:
    """把最小货币单位转成展示金额（如 6976 -> 69.76，0 -> 0.0）。"""
    if minor is None:
        return None
    if str(currency or "").strip().upper() in _ZERO_DECIMAL_CURRENCIES:
        return float(minor)
    return round(minor / 100.0, 2)


def parse_checkout_pricing(detail: dict | None,
                           fallback: dict | None = None) -> dict:
    """从 checkout 响应解析本次应付价格并判断是否首月免费（支付价 == 0）。

    优先读 GET 详情的 checkout_state.total.total；缺失时回退到 POST 创建响应。
    """
    out: dict[str, Any] = {
        "free_trial": None,
        "checkout_total_minor": None,
        "checkout_total_amount": None,
        "checkout_currency": "",
        "checkout_campaign": "",
    }
    sources = [s for s in (detail, fallback) if isinstance(s, dict)]
    if not sources:
        return out

    currency = ""
    for src in sources:
        state = src.get("checkout_state") if isinstance(src.get("checkout_state"), dict) else {}
        bd = src.get("billing_details") if isinstance(src.get("billing_details"), dict) else {}
        cur = str(state.get("currency") or bd.get("currency") or "").strip().upper()
        if cur:
            currency = cur
            break
    out["checkout_currency"] = currency

    for src in sources:
        camp = src.get("promo_campaign")
        if isinstance(camp, dict):
            cid = str(camp.get("promo_campaign_id") or camp.get("id") or camp.get("name") or "").strip()
            if cid:
                out["checkout_campaign"] = cid
                break
        elif isinstance(camp, str) and camp.strip():
            out["checkout_campaign"] = camp.strip()
            break

    total_minor: int | None = None
    for src in sources:
        state = src.get("checkout_state") if isinstance(src.get("checkout_state"), dict) else {}
        tot_box = state.get("total") if isinstance(state.get("total"), dict) else {}
        cand = _extract_minor_amount(tot_box.get("total"), currency)
        if cand is not None:
            total_minor = cand
            break

    if total_minor is not None:
        out["checkout_total_minor"] = total_minor
        out["checkout_total_amount"] = _minor_to_major(total_minor, currency)
        out["free_trial"] = (total_minor == 0)
    return out


def read_checkout_detail(env: Any, headers: dict, session_id: str,
                         timeout: float = 45.0) -> dict | None:
    """读 checkout 详情完整 JSON；失败返回 None。

    必须带 supports_business_seat_offer=true，否则 business_minimum_seats 不返回。
    """
    url = CHECKOUT_DETAIL.format(sid=session_id) + "?supports_business_seat_offer=true"
    try:
        resp = env.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 探测失败不该打断主流程
        logger.debug("[单席位] 读详情失败：%s", exc)
        return None
    if resp.status_code != 200:
        logger.debug("[单席位] 读详情 HTTP %s", resp.status_code)
        return None
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def read_minimum_seats(env: Any, headers: dict, session_id: str,
                       timeout: float = 45.0) -> int | None:
    """读 business_minimum_seats；读不到返回 None。

    必须用 supports_business_seat_offer=true，否则字段不返回。
    """
    url = CHECKOUT_DETAIL.format(sid=session_id) + "?supports_business_seat_offer=true"
    try:
        resp = env.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 探测失败不该打断主流程
        logger.debug("[单席位] 读详情失败：%s", exc)
        return None
    if resp.status_code != 200:
        logger.debug("[单席位] 读详情 HTTP %s", resp.status_code)
        return None
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    value = data.get("business_minimum_seats")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def update_seat_quantity(env: Any, headers: dict, created: dict, seats: int,
                         promo: str = "", timeout: float = 45.0) -> tuple[bool, str]:
    """把已创建的 session 改成指定席位数。

    返回 (是否成功, 失败原因)。服务端不允许时会明确回
    400 seat_quantity must be at least 2。
    """
    body = {
        "checkout_session_id": created.get("checkout_session_id"),
        "processor_entity": created.get("processor_entity"),
        "plan_name": created.get("plan_name"),
        "price_interval": "month",
        "seat_quantity": seats,
        "seat_quantities": [
            {"seat_type": "default", "quantity": seats},
            {"seat_type": "prolite", "quantity": 0},
        ],
        "promo_campaign": None,
    }
    if promo:
        body["promo_code"] = promo
    try:
        resp = env.post(CHECKOUT_UPDATE, headers=headers,
                        data=json.dumps(body), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {(resp.text or '')[:160]}"
    try:
        ok = bool((resp.json() or {}).get("success"))
    except Exception:  # noqa: BLE001
        ok = True
    return (True, "") if ok else (False, "服务端未返回 success")


def probe_single_seat(account: dict, *, timeout: float = 45.0) -> dict:
    """探测这个账号能不能只买 1 席。

    只创建 checkout session 读取限制，不改席位、不支付。创建 session
    本身不扣款，也不消耗优惠码（这里不传 promo）。

    返回 {ok, supported, minimum_seats, country, currency, error}。
    """
    from config.proxy import normalize_proxy_url
    from core.chatgpt_plan import normalize_token, token_claims
    from core.session import BrowserSession
    from core.team_checkout_service import COUNTRY_CURRENCY

    out = {"ok": False, "supported": None, "minimum_seats": None,
           "free_trial": None, "checkout_total_minor": None,
           "checkout_total_amount": None, "checkout_currency": "",
           "checkout_campaign": "",
           "country": "", "currency": "", "error": ""}

    token = normalize_token(str(account.get("access_token") or ""))
    if not token:
        out["error"] = "没有 access_token"
        return out
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        out["error"] = "access_token 已过期，请先查活刷新"
        return out

    proxy = str(account.get("proxy_used") or "")
    region = str(account.get("proxy_region") or "")

    # 1024proxy 注册的号，注册出口是临时 IP（保留 120 分钟就失效）。
    # 继续拿它探席位必然连不通，而连不通的表现是探不出出口国家，
    # 最终报成 "出口国家 未知 没有内置货币映射" —— 看着像账号问题，
    # 其实是出口问题（2026-11-18：#109/#121 都栽在这里）。
    #
    # 和查活用同一套处理：按注册地区换一个代理池的长期出口。
    # 地区会影响定价和币种，所以优先同地区，没有才回落 US。
    try:
        from core.region_proxy import adopt_pool_exit
        picked, why = adopt_pool_exit(
            int(account.get("id") or 0), proxy, region, label="席位探测")
        if picked and picked != proxy:
            logger.info("[单席位] 注册用的是临时出口，改用代理池：%s", why)
            proxy = picked
    except Exception:  # noqa: BLE001 - 选路失败就沿用原出口，别让探测起不来
        logger.warning("[单席位] 按地区选出口失败，沿用原出口", exc_info=True)

    env = BrowserSession(
        proxy=normalize_proxy_url(proxy) if proxy else None,
        detect_exit_geo=True,
        fingerprint_seed=f"seat-probe:{uuid.uuid4()}")
    try:
        country = str((env.exit_geo or {}).get("country") or "").upper()
        currency = COUNTRY_CURRENCY.get(country, "")
        if not country or not currency:
            out["error"] = f"出口国家 {country or '未知'} 没有内置货币映射"
            return out
        out["country"], out["currency"] = country, currency

        # 代理偶发瞬断，预热重试几次再放弃
        warmed = False
        for _ in range(3):
            try:
                env.get("https://chatgpt.com/",
                        headers=env.get_chatgpt_navigate_headers(
                            referer="https://chatgpt.com/", user_initiated=False),
                        allow_redirects=True, timeout=15)
                warmed = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
        if not warmed:
            out["error"] = "预热 chatgpt.com 失败（代理不通）"
            return out
        # 预热阶段的 403 不代表接口不可用，清掉熔断继续
        env.blocked_until = 0.0
        env.blocked_reason = ""

        headers = checkout_headers(env, token, claims)
        attempt_id = str(uuid.uuid4())
        payload = build_create_payload(
            f"probe-{attempt_id[:8]}", 2, country, currency,
            attempt_id=attempt_id)
        try:
            resp = env.post(CHECKOUT_CREATE, headers=headers,
                            data=json.dumps(payload), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"创建 checkout 失败：{type(exc).__name__}: {exc}"
            return out
        if resp.status_code != 200:
            out["error"] = f"创建 checkout HTTP {resp.status_code}: {(resp.text or '')[:160]}"
            return out
        try:
            created = resp.json() or {}
            sid = str(created.get("checkout_session_id") or "")
        except Exception:  # noqa: BLE001
            out["error"] = "创建响应不是 JSON"
            return out
        if not sid:
            out["error"] = "创建成功但没拿到 checkout_session_id"
            return out

        detail = read_checkout_detail(env, headers, sid, timeout=timeout)
        minimum = None
        if isinstance(detail, dict):
            try:
                minimum = int(detail.get("business_minimum_seats"))
            except (TypeError, ValueError):
                minimum = None
        if minimum is None:
            out["error"] = "读不到 business_minimum_seats"
            return out
        pricing = parse_checkout_pricing(detail, fallback=created)
        out["ok"] = True
        out["minimum_seats"] = minimum
        out["supported"] = minimum <= 1
        out["free_trial"] = pricing["free_trial"]
        out["checkout_total_minor"] = pricing["checkout_total_minor"]
        out["checkout_total_amount"] = pricing["checkout_total_amount"]
        out["checkout_currency"] = pricing["checkout_currency"] or currency
        out["checkout_campaign"] = pricing["checkout_campaign"]
        logger.info(
            "[单席位] 账号 %s 探测结果：min=%s 支持1席=%s 首月免费=%s 支付价=%s %s（出口 %s/%s）",
            account.get("email") or account.get("id"),
            minimum, out["supported"], out["free_trial"],
            out["checkout_total_amount"], out["checkout_currency"],
            country, currency,
        )
        return out
    finally:
        try:
            env.close()
        except Exception:  # noqa: BLE001
            pass
