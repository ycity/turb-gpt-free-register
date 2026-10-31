# -*- coding: utf-8 -*-
"""Team 支付长链接生成服务（jinmotech 接口 + 1024proxy + 优惠码池）。"""
import os
import re
import json
import logging
import sqlite3
import datetime
import urllib.request
import urllib.error
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).resolve().parents[1] / "turb.sqlite3")
DEFAULT_1024PROXY_API = "https://white.1024proxy.com/white/api?region=EG&num=1&time=10&format=1&type=txt"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
PROMO_ERROR_KEYWORDS = ("promo", "coupon", "discount", "优惠", "折扣", "促销")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'unused',
            account_id INTEGER,
            account_email TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_promo_status ON promo_codes(status)")
    return conn


def extract_clean_promo_code(raw: str) -> str:
    """从纯优惠码或带 promo/coupon 参数的链接中提取优惠码。"""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    m = re.search(r'(?:promo|promo_code|coupon|code)=([A-Za-z0-9_-]+)', raw, re.I)
    if m:
        return m.group(1).strip()
    m_code = re.search(r'([A-Za-z0-9_-]{4,})', raw)
    if m_code:
        return m_code.group(1).strip()
    return raw


# ----------------- 优惠码池 -----------------

def get_promo_stats() -> dict:
    with _connect() as conn:
        rows = {r[0]: r[1] for r in conn.execute("SELECT status, count(*) FROM promo_codes GROUP BY status")}
    return {
        "total": sum(rows.values()),
        "unused": rows.get("unused", 0),
        "using": rows.get("using", 0),
        "used": rows.get("used", 0),
        "expired": rows.get("expired", 0),
    }


def list_promo_codes(limit: int = 100, status: str = "") -> list[dict]:
    with _connect() as conn:
        if status:
            rows = conn.execute("SELECT * FROM promo_codes WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = conn.execute("SELECT * FROM promo_codes ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def import_promo_codes(lines: list[str]) -> dict:
    inserted = duplicates = 0
    now = _now()
    with _connect() as conn:
        for line in lines:
            code = extract_clean_promo_code(line)
            if not code:
                continue
            try:
                conn.execute(
                    "INSERT INTO promo_codes (code, status, created_at, updated_at) VALUES (?, 'unused', ?, ?)",
                    (code, now, now),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                duplicates += 1
    return {"inserted": inserted, "duplicates": duplicates, "stats": get_promo_stats()}


def claim_next_promo_code(account_id: int | None = None, account_email: str | None = None) -> dict | None:
    """原子领取一个未使用的优惠码并标记为 using，避免并发提链时重复分派。"""
    with _connect() as conn:
        row = conn.execute(
            """
            UPDATE promo_codes
            SET status = 'using', account_id = ?, account_email = ?, updated_at = ?
            WHERE id = (SELECT id FROM promo_codes WHERE status = 'unused' ORDER BY id ASC LIMIT 1)
            RETURNING *
            """,
            (account_id, account_email, _now()),
        ).fetchone()
    return dict(row) if row else None


def mark_promo_code(promo_id: int, status: str, account_id: int | None = None,
                    account_email: str | None = None, error: str | None = None):
    with _connect() as conn:
        conn.execute(
            """
            UPDATE promo_codes
            SET status = ?,
                account_id = CASE WHEN ? = 'unused' THEN NULL ELSE coalesce(?, account_id) END,
                account_email = CASE WHEN ? = 'unused' THEN NULL ELSE coalesce(?, account_email) END,
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, status, account_id, status, account_email, error, _now(), promo_id),
        )


def clear_promo_codes(status: str = "") -> int:
    with _connect() as conn:
        if status:
            cur = conn.execute("DELETE FROM promo_codes WHERE status = ?", (status,))
        else:
            cur = conn.execute("DELETE FROM promo_codes")
        return cur.rowcount


# ----------------- 1024proxy -----------------

def fetch_1024proxy_ips(region: str = "EG", num: int = 4, api_url: str = "") -> list[str]:
    """从 1024proxy 白名单 API 拉取多个与 region 匹配的 IP:Port，便于跳过粘滞的死出口。"""
    from urllib.parse import urlsplit, parse_qsl, urlencode
    base = (api_url or os.getenv("PROXIES_1024_API_URL") or DEFAULT_1024PROXY_API).strip()
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if region:
        query["region"] = region.strip().upper()
    query["num"] = str(max(1, int(num)))
    url = parts._replace(query=urlencode(query)).geturl()
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        text = resp.read().decode("utf-8", "replace").strip()
    found = list(dict.fromkeys(re.findall(r'(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})', text)))
    if not found:
        raise ValueError(f"1024proxy 返回格式非预期: {text[:120]}")
    return found


def fetch_1024proxy_ip(api_url: str = "", region: str = "EG") -> str:
    return fetch_1024proxy_ips(region=region, num=1, api_url=api_url)[0]


# ----------------- 提链 -----------------

CHECKOUT_API = "https://chatgpt.com/backend-api/payments/checkout"
PROVIDERS = ("native", "jinmotech")
_EUR_COUNTRIES = "AT BE CY DE EE ES FI FR GR HR IE IT LT LU LV MT NL PT SI SK"
COUNTRY_CURRENCY = {
    **{c: "EUR" for c in _EUR_COUNTRIES.split()},
    "US": "USD", "GB": "GBP", "AU": "AUD", "CA": "CAD", "NZ": "NZD", "EG": "EGP", "CL": "CLP",
    "IN": "INR", "JP": "JPY", "KR": "KRW", "SG": "SGD", "HK": "HKD", "TW": "TWD", "BR": "BRL",
    "MX": "MXN", "TR": "USD", "NG": "NGN", "PH": "PHP", "ID": "IDR", "MY": "MYR", "TH": "THB",
    "VN": "VND", "PK": "PKR", "ZA": "ZAR", "KE": "USD", "CO": "COP", "PE": "PEN", "AR": "USD",
    "SA": "SAR", "AE": "AED", "IL": "ILS", "PL": "PLN", "CH": "CHF", "SE": "SEK", "NO": "NOK",
    "DK": "DKK", "CZ": "CZK", "HU": "HUF", "RO": "RON",
}


def _error_message(body: str, fallback: str) -> str:
    try:
        data = json.loads(body)
    except Exception:
        return (body or "").strip()[:300] or fallback
    if isinstance(data, dict):
        err = data.get("error") or data.get("message") or data.get("msg") or data.get("detail")
        if isinstance(err, dict):
            code = err.get("code")
            err = err.get("message") or json.dumps(err, ensure_ascii=False)
            if code:
                err = f"{err} [{code}]"
        if err:
            return str(err)[:300]
    return json.dumps(data, ensure_ascii=False)[:300]


def _normalize_proxy(proxy: str) -> str:
    """统一成 jinmotech 接受的 host:port[:user:pass]。"""
    proxy = (proxy or "").strip()
    if "://" not in proxy:
        return proxy
    from urllib.parse import urlparse
    u = urlparse(proxy)
    if u.username and u.password:
        return f"{u.hostname}:{u.port}:{u.username}:{u.password}"
    return f"{u.hostname}:{u.port}"


def _jinmotech_checkout(token: str, proxy: str, workspace: str, seats: int, country: str,
                        promo: str, timeout: int) -> tuple[str | None, dict | None, str]:
    payload = {
        "token": token,
        "workspace": workspace,
        "seats": seats,
        "country": country,
        "promoCode": promo,
        "proxyMode": "custom",
        "proxyAddress": _normalize_proxy(proxy),
    }
    req = urllib.request.Request(
        "https://team.jinmotech.com/api/generate-link",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": BROWSER_UA,
            "Origin": "https://team.jinmotech.com",
            "Referer": "https://team.jinmotech.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return None, None, _error_message(e.read().decode("utf-8", "replace"), f"HTTP {e.code}")
    try:
        data = json.loads(body)
    except Exception:
        data = None
    url = (data or {}).get("url") or (data or {}).get("checkoutUrl") or (data or {}).get("link")
    return url, data, "" if url else _error_message(body, "接口未返回链接")


def _native_checkout(token: str, proxy: str, workspace: str, seats: int, country: str,
                     currency: str, promo: str, timeout: int) -> tuple[str | None, dict | None, str]:
    """本地直连 chatgpt.com 创建 Team 订阅的 Stripe hosted checkout。proxy 为空时用项目代理池。"""
    from config.proxy import normalize_proxy_url
    from core.chatgpt_plan import token_claims, _warm_plan_session
    from core.session import BrowserSession

    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return None, None, "访问令牌已过期，请先查活刷新 AT [TOKEN_EXPIRED]"

    payload = {
        "plan_name": "chatgptteamplan",
        "team_plan_data": {"workspace_name": workspace, "price_interval": "month", "seat_quantity": seats},
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/",
        "checkout_ui_mode": "hosted",
    }
    if promo:
        payload["promo_code"] = promo

    seed = f"team-checkout:{claims.get('email') or token[:24]}:{uuid.uuid4()}"
    env = BrowserSession(proxy=normalize_proxy_url(proxy) if proxy else None,
                         detect_exit_geo=False, fingerprint_seed=seed)
    try:
        resp_warm = env.get(
            "https://chatgpt.com/",
            headers=env.get_chatgpt_navigate_headers(referer="https://chatgpt.com/", user_initiated=False),
            allow_redirects=True,
            timeout=10,
        )
        env.blocked_until = 0.0
        env.blocked_reason = ""
        headers = env.get_chatgpt_headers(referer="https://chatgpt.com/")
        headers["authorization"] = f"Bearer {token}"
        if claims.get("account_id"):
            headers["chatgpt-account-id"] = str(claims["account_id"])
        resp = env.post(CHECKOUT_API, headers=headers, data=json.dumps(payload), timeout=timeout)
        status = int(resp.status_code)
        body = resp.text or ""
    finally:
        env.close()

    try:
        data = json.loads(body)
    except Exception:
        data = None
    if 200 <= status < 300 and isinstance(data, dict):
        url = data.get("url") or data.get("stripe_hosted_url") or data.get("checkout_url")
        if url:
            return url, data, ""
    if body.lstrip().startswith("<"):
        msg = "chatgpt.com 返回了 HTML 页面，多半被 Cloudflare 拦截，换个代理 IP 再试"
    else:
        msg = _error_message(body, "接口未返回链接")
    return None, data, f"{msg} [HTTP {status}]"


def generate_team_checkout_link(
    token: str,
    proxy_address: str = "",
    workspace: str = "MyTeam",
    seats: int = 2,
    country: str = "EG",
    currency: str = "",
    promo_code: str = "",
    use_1024proxy: bool = True,
    provider: str = "native",
    account_id: int | None = None,
    account_email: str | None = None,
    timeout: int = 45,
) -> dict:
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "accessToken 不能为空"}
    if provider not in PROVIDERS:
        return {"ok": False, "error": f"未知提链方式: {provider}"}

    workspace = (workspace or "MyTeam").strip()
    seats = max(2, int(seats or 2))
    country = (country or "EG").strip().upper()
    currency = (currency or COUNTRY_CURRENCY.get(country, "")).strip().upper()
    if provider == "native" and not currency:
        return {"ok": False, "error": f"国家 {country} 没有内置货币映射，请手动填写货币代码（如 USD）"}

    proxy = (proxy_address or "").strip()
    proxy_candidates: list[str] = []
    if proxy:
        proxy_candidates = [proxy]
    elif use_1024proxy:
        try:
            proxy_candidates = fetch_1024proxy_ips(region=country, num=4)
        except Exception as e:
            return {"ok": False, "error": f"1024proxy 取 IP 失败: {e}"}
    elif provider == "jinmotech":
        return {"ok": False, "error": "jinmotech 方式必须提供代理：请勾选 1024proxy 或填写自定义代理"}
    else:
        proxy_candidates = [""]

    assigned = None
    promo = (promo_code or "").strip()
    if promo:
        promo = extract_clean_promo_code(promo)
    else:
        assigned = claim_next_promo_code(account_id, account_email)
        if assigned:
            promo = assigned["code"]
            logger.info("优惠码池分派 %s (id=%s) 给账号 %s", promo, assigned["id"], account_id)

    url = data = None
    err_msg = ""
    used_proxy = proxy_candidates[0] if proxy_candidates else ""
    per_attempt_timeout = 18 if len(proxy_candidates) > 1 else timeout
    for idx, cand in enumerate(proxy_candidates, 1):
        used_proxy = cand
        try:
            if provider == "native":
                url, data, err_msg = _native_checkout(token, cand, workspace, seats, country, currency, promo, per_attempt_timeout)
            else:
                url, data, err_msg = _jinmotech_checkout(token, cand, workspace, seats, country, promo, per_attempt_timeout)
        except Exception as e:
            url, data, err_msg = None, None, f"{type(e).__name__}: {e}"
        if url:
            break
        low_err = (err_msg or "").lower()
        retryable = any(k in low_err for k in ("timeout", "timed out", "connectionerror", "proxy", "cloudflare", "html", "curl: (7)", "curl: (28)", "curl: (35)", "curl: (56)"))
        if not retryable or idx >= len(proxy_candidates):
            break
        logger.warning("Team 提链第 %s/%s 个代理出口 (%s) 不可用 (%s)，自动切换下一个出口...", idx, len(proxy_candidates), cand, err_msg[:100])

    meta = {
        "provider": provider,
        "promo_used": promo,
        "promo_from_pool": bool(assigned),
        "proxy_used": used_proxy or "项目代理池",
        "country": country,
        "currency": currency,
    }

    if url:
        if assigned:
            mark_promo_code(assigned["id"], "used", account_id, account_email)
        return {"ok": True, "url": url, "data": data, **meta}

    logger.warning("Team 提链失败 provider=%s account=%s promo=%s proxy=%s: %s",
                   provider, account_id, promo, meta["proxy_used"], err_msg)
    if assigned:
        promo_issue = any(k in err_msg.lower() for k in PROMO_ERROR_KEYWORDS)
        mark_promo_code(assigned["id"], "expired" if promo_issue else "unused", error=err_msg)
    return {"ok": False, "error": err_msg, **meta}
