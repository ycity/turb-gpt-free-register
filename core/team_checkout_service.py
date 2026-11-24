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
from urllib.parse import urlsplit

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
    """从纯优惠码或分享链接中提取优惠码。

    支持三种形态：
      - 带 promo= / coupon= 等查询参数的链接
      - https://chatgpt.com/p/<CODE> 这类把码放在路径末段的分享链接
      - 纯优惠码
    """
    raw = str(raw or "").strip()
    if not raw:
        return ""
    m = re.search(r'(?:promo|promo_code|coupon|code)=([A-Za-z0-9_-]+)', raw, re.I)
    if m:
        return m.group(1).strip()
    # 链接的 scheme/域名同样由字母数字组成，不能直接取第一段匹配，
    # 否则 https://chatgpt.com/p/XXXX 会被提取成 "https"。
    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', raw) or raw.lower().startswith("chatgpt.com/"):
        path = urlsplit(raw if "://" in raw else f"https://{raw}").path
        segment = next(
            (part for part in reversed(path.split("/")) if part.strip()), ""
        )
        return segment.strip()
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


def reset_promo_code_by_code(code: str) -> bool:
    """把误登记为 used 的优惠码按码重置回 unused（支付未生效回滚用）。

    只动仍处于 used 的行：若已被人工改过状态，不覆盖人工决定。
    """
    clean = extract_clean_promo_code(code or "")
    if not clean:
        return False
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE promo_codes
            SET status = 'unused', account_id = NULL, account_email = NULL,
                updated_at = ?
            WHERE code = ? AND status = 'used'
            """,
            (_now(), clean),
        )
        return cur.rowcount > 0


def record_used_promo_code(code: str, account_id: int | None = None,
                           account_email: str | None = None,
                           status: str = "used") -> dict:
    """把手动填写并实际用掉的优惠码登记进池，方便以后查阅。

    手动填的码不经过 claim_next_promo_code，原先用完就没了记录，下次翻不到
    是谁用了哪个码。这里补登：池里没有就插入，已存在就补上账号归属。

    已经是 used 的记录不会被覆盖——同一个码重复登记时保留首次的归属，避免
    把真实使用者改掉。
    """
    clean = extract_clean_promo_code(code or "")
    if not clean:
        return {"ok": False, "error": "优惠码为空或格式无法识别"}

    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM promo_codes WHERE code = ?", (clean,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO promo_codes (code, status, account_id, account_email,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (clean, status, account_id, account_email, now, now))
            return {"ok": True, "code": clean, "action": "inserted",
                    "stats": get_promo_stats()}

        before = dict(row)
        if str(before.get("status") or "") == "used":
            return {"ok": True, "code": clean, "action": "already_used",
                    "account_email": before.get("account_email"),
                    "stats": get_promo_stats()}

        conn.execute(
            "UPDATE promo_codes SET status = ?,"
            " account_id = coalesce(?, account_id),"
            " account_email = coalesce(?, account_email),"
            " updated_at = ? WHERE id = ?",
            (status, account_id, account_email, now, before["id"]))
        return {"ok": True, "code": clean, "action": "updated",
                "previous_status": before.get("status"),
                "stats": get_promo_stats()}


def reclaim_stale_promo_codes(older_than_minutes: float = 30.0) -> dict:
    """把卡在 using 的码退回 unused。

    码在提链时标成 using，支付成功才升 used。支付失败、被金额闸门拦下、
    或会话超时被回收的，码会一直挂在 using——但 OpenAI 侧并没有消耗它，
    所以应该退回池子继续用。

    只退回超过 older_than_minutes 的：刚领走的可能正在支付中，退回去会
    被另一个流程重复分派。
    """
    import datetime

    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(minutes=max(0.0, older_than_minutes)))
    cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    freed = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, code, account_id, updated_at FROM promo_codes"
            " WHERE status = 'using'").fetchall()
        for r in rows:
            if str(r["updated_at"] or "")[:19] >= cutoff_s:
                continue           # 可能正在支付，先不动
            conn.execute(
                "UPDATE promo_codes SET status='unused', account_id=NULL,"
                " account_email=NULL, last_error=?, updated_at=? WHERE id=?",
                ("提链后未完成支付，码未被消耗，已自动退回池", _now(), r["id"]))
            freed.append(str(r["code"]))

    if freed:
        logger.info("[优惠码] 退回 %d 个未完成支付的码：%s",
                    len(freed), ",".join(freed))
    return {"freed": freed, "count": len(freed), "stats": get_promo_stats()}


def release_promo_code(code: str, reason: str = "") -> bool:
    """把还挂在 using 的码退回 unused（流程失败、码没随支付消耗时调用）。

    2026-11-20 补跑事故的另一半：失败候选的码一直挂在 using（
    reclaim_stale_promo_codes 写了但从未被调用），把整个池子堵死，
    后面的候选按原价跑完全程才被金额闸门拦下。

    只动仍处于 using 的行：若支付实际提交成功过，_record_promo 已把它
    升成 used，说明码已被 OpenAI 消耗，绝不能退回，否则会二次分派废码。
    幂等：重复调用、码不在 using 时都是 no-op。
    """
    clean = extract_clean_promo_code(code or "")
    if not clean:
        return False
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE promo_codes
            SET status = 'unused', account_id = NULL, account_email = NULL,
                last_error = ?, updated_at = ?
            WHERE code = ? AND status = 'using'
            """,
            (reason or "流程未完成支付，码未被消耗，退回池子", _now(), clean),
        )
        freed = cur.rowcount > 0
    if freed:
        logger.info("[优惠码] %s 未随支付消耗，已退回池子（%s）", clean, reason)
    return freed


def clear_promo_codes(status: str = "") -> int:
    with _connect() as conn:
        if status:
            cur = conn.execute("DELETE FROM promo_codes WHERE status = ?", (status,))
        else:
            cur = conn.execute("DELETE FROM promo_codes")
        return cur.rowcount


# ----------------- 1024proxy -----------------

def fetch_1024proxy_ips(region: str = "EG", num: int = 4, api_url: str = "",
                        minutes: int = 0) -> list[str]:
    """从 1024proxy 白名单 API 拉取多个与 region 匹配的 IP:Port，便于跳过粘滞的死出口。

    minutes 是代理的保留时长。默认 URL 里是 time=10，对提链够用，
    但支付要先登录拿 cookie、填表、等人点验证码，10 分钟常常不够——
    代理中途失效会让后面每一步都莫名其妙地失败。
    传 minutes>0 覆盖它。
    """
    from urllib.parse import urlsplit, parse_qsl, urlencode
    base = (api_url or os.getenv("PROXIES_1024_API_URL") or DEFAULT_1024PROXY_API).strip()
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if region:
        query["region"] = region.strip().upper()
    query["num"] = str(max(1, int(num)))
    if minutes and int(minutes) > 0:
        query["time"] = str(int(minutes))
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


def _registration_exit_geo(proxy: str, account_proxy: str = "") -> dict:
    """取本次提链出口的地理信息，用于决定结账页语言。

    代理池在加入时已经测过出口 IP 归属地，直接复用那份结果，不再发 geo 请求。
    优先用本次实际使用的代理；没测过时退回账号注册时用的代理。
    """
    from webui import proxy_pool as _pool

    for candidate in (proxy, account_proxy):
        if not str(candidate or "").strip():
            continue
        try:
            geo = _pool.tested_geo(candidate)
        except Exception:
            logger.debug("[Team] 读取代理出口地理失败: %s", candidate, exc_info=True)
            continue
        if geo:
            return geo
    return {}


def _single_seat_enabled() -> bool:
    """是否尝试 1 席。默认开，出问题可在配置里关掉退回 2 席。"""
    try:
        from config import register as cfg
        return bool(getattr(cfg, "TEAM_TRY_SINGLE_SEAT", True))
    except Exception:  # noqa: BLE001 - 读不到配置就按开启处理
        return True


def _try_single_seat(env, headers: dict, workspace: str, country: str,
                     currency: str, promo: str, timeout: int) -> tuple[str, dict] | None:
    """尝试建一条 1 席的结账链接；不支持就返回 None。

    1 席只能走 custom 模式再 update：hosted 模式对任何账号都直接回
    400 must purchase a minimum of 2 seats，哪怕该账号的
    business_minimum_seats 已经是 1（实测验证过）。

    custom 模式不返回 pay.openai.com 长链，支付入口是
    chatgpt.com/checkout/openai_llc/{id}，实测能正常打开并显示金额。
    """
    from core.single_seat import (build_create_payload, read_minimum_seats,
                                  update_seat_quantity)

    attempt_id = str(uuid.uuid4())
    payload = build_create_payload(workspace, 2, country, currency,
                                   promo=promo, attempt_id=attempt_id)
    try:
        resp = env.post(CHECKOUT_API, headers=headers,
                        data=json.dumps(payload), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 探测失败就退回 2 席
        logger.debug("[单席位] 创建失败：%s", exc)
        return None
    if resp.status_code != 200:
        logger.debug("[单席位] 创建 HTTP %s", resp.status_code)
        return None
    try:
        created = resp.json()
    except Exception:  # noqa: BLE001
        return None
    sid = str(created.get("checkout_session_id") or "")
    if not sid:
        return None

    minimum = read_minimum_seats(env, headers, sid, timeout=timeout)
    if minimum is None or minimum > 1:
        logger.info("[单席位] 该账号最少 %s 席，按 2 席走", minimum if minimum else "未知")
        return None

    ok, err = update_seat_quantity(env, headers, created, 1,
                                   promo=promo, timeout=timeout)
    if not ok:
        logger.warning("[单席位] min=1 但改席位失败，退回 2 席：%s", err[:160])
        return None

    url = f"https://chatgpt.com/checkout/openai_llc/{sid}"
    created["single_seat"] = True
    created["checkout_url"] = url
    logger.warning("[单席位] 已生成 1 席结账链接（省一半订阅费）：%s", sid)
    return url, created


def _native_checkout(token: str, proxy: str, workspace: str, seats: int, country: str,
                     currency: str, promo: str, timeout: int,
                     account_proxy: str = "") -> tuple[str | None, dict | None, str]:
    """本地直连 chatgpt.com 创建 Team 订阅的 Stripe hosted checkout。proxy 为空时用项目代理池。"""
    from config.proxy import normalize_proxy_url
    from core.chatgpt_plan import token_claims, _warm_plan_session
    from core.session import BrowserSession, _seeded_browser_profile

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
    # 结账页按 Accept-Language / oai-locale 渲染。不带地理信息时会落到全局默认
    # 画像（日语），所以用代理池测好的出口地区生成画像，让页面语言跟随出口。
    geo = _registration_exit_geo(proxy, account_proxy)
    profile = _seeded_browser_profile(seed, geo) if geo else None
    if geo:
        logger.info("[Team] 结账语言跟随出口地区：country=%s city=%s",
                    geo.get("country"), geo.get("city") or "-")
    env = BrowserSession(proxy=normalize_proxy_url(proxy) if proxy else None,
                         detect_exit_geo=False, fingerprint_seed=seed,
                         browser_profile=profile)
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

        # 服务端对部分账号放开了 1 席，能用就省一半订阅费。
        # 探测要带齐 Codex 那套头，否则 business_minimum_seats 字段根本不返回。
        if _single_seat_enabled() and seats <= 2:
            from core.single_seat import CODEX_HEADERS
            probe_headers = dict(headers)
            probe_headers.update(CODEX_HEADERS)
            probe_headers["content-type"] = "application/json"
            try:
                hit = _try_single_seat(env, probe_headers, workspace, country,
                                       currency, promo, timeout)
            except Exception:  # noqa: BLE001 - 单席位是增值路径，失败就走原路
                logger.exception("[单席位] 探测异常，按 2 席继续")
                hit = None
            if hit:
                return hit[0], hit[1], ""

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


def _maybe_auto_pay(url: str, account_email: str, proxy: str) -> dict | None:
    """生成长链后按配置尝试 VCC 自动支付。

    支付失败绝不影响提链结果：长链本身已经拿到，这里只附带一个 auto_pay 字段。

    注意：单席位（elements）结账页必须有登录态才打得开，而这条路径用的是
    裸 pay_checkout、不带登录 cookie，打开只会落到 /auth/login。需要支付的
    调用方（如自动订阅）应当传 auto_pay=False，改由 vcc_pay_session 那条
    会先登录注入 session-token 的路径来付——否则同一个长链会被匿名会话
    先打开一次，白烧一张链。
    """
    try:
        from config import vcc as vcc_cfg
    except Exception as exc:  # noqa: BLE001
        logger.debug("VCC 配置不可用，跳过自动支付：%s", exc)
        return None
    if not getattr(vcc_cfg, "VCC_AUTO_PAY_ENABLED", False):
        return None
    if not account_email:
        logger.warning("VCC 自动支付需要账号邮箱，当前为空，已跳过")
        return {"ok": False, "stage": "precheck", "message": "缺少账号邮箱"}
    try:
        from core.vcc_checkout_pay import pay_checkout
        return pay_checkout(url, email=account_email, proxy=proxy, cfg=vcc_cfg)
    except Exception as exc:  # noqa: BLE001 - 支付异常不能吃掉长链
        logger.exception("VCC 自动支付异常")
        return {"ok": False, "stage": "exception", "message": f"{type(exc).__name__}: {exc}"}


def generate_team_checkout_link(
    token: str,
    proxy_address: str = "",
    workspace: str = "MyTeam",
    seats: int = 2,
    country: str = "EG",
    currency: str = "",
    promo_code: str = "",
    promo_assigned: dict | None = None,
    use_1024proxy: bool = True,
    provider: str = "native",
    account_id: int | None = None,
    account_email: str | None = None,
    account_proxy: str = "",
    timeout: int = 45,
    auto_pay: bool = True,
    proxy_candidates: list[str] | None = None,
) -> dict:
    """生成长链。auto_pay=False 时只管提链，不做任何支付尝试。

    自动订阅必须传 False：它随后会用 vcc_pay_session 支付，那条路会先登录
    并注入 session-token。两个都付等于同一张链被匿名会话先打开一次，白烧。

    promo_assigned：调用方已经从池里领好的码行（自动订阅的前置闸门在
    选卡之前领码）。走 assigned 通道——成功留在 using、提链失败自动退回，
    和内部领码完全同款；不会像显式 promo_code 那样在提链时就被记成 used。

    proxy_candidates：调用方已验过国别的备选出口列表。传入时按序兜底切换
    （proxy_address 若非空则排在最前），不再重复调 1024proxy，避免首选口
    刚好断气就把整单拖死。
    """
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
    explicit_candidates = [
        str(p or "").strip() for p in (proxy_candidates or []) if str(p or "").strip()
    ]
    resolved_candidates: list[str] = []
    if explicit_candidates:
        resolved_candidates = list(dict.fromkeys(
            ([proxy] if proxy else []) + explicit_candidates
        ))
    elif proxy:
        resolved_candidates = [proxy]
    elif use_1024proxy:
        try:
            # 提链本身很快，但同一条链接着就要拿去支付（登录、填表、
            # 等人点验证码），默认 10 分钟常常不够，统一按配置拉长。
            try:
                from config import vcc as _vcc
                _mins = int(getattr(_vcc, "VCC_1024PROXY_MINUTES", 0) or 0)
            except Exception:  # noqa: BLE001 - 配置缺失不该挡住提链
                _mins = 0
            resolved_candidates = fetch_1024proxy_ips(region=country, num=4,
                                                      minutes=_mins)
        except Exception as e:
            return {"ok": False, "error": f"1024proxy 取 IP 失败: {e}"}
    elif provider == "jinmotech":
        return {"ok": False, "error": "jinmotech 方式必须提供代理：请勾选 1024proxy 或填写自定义代理"}
    else:
        resolved_candidates = [""]

    assigned = None
    if isinstance(promo_assigned, dict) and promo_assigned.get("id"):
        assigned = promo_assigned
    promo = (promo_code or "").strip()
    if promo:
        promo = extract_clean_promo_code(promo)
    elif assigned:
        promo = extract_clean_promo_code(str(assigned.get("code") or ""))
    else:
        assigned = claim_next_promo_code(account_id, account_email)
        if assigned:
            promo = assigned["code"]
            logger.info("优惠码池分派 %s (id=%s) 给账号 %s", promo, assigned["id"], account_id)

    url = data = None
    err_msg = ""
    used_proxy = resolved_candidates[0] if resolved_candidates else ""
    per_attempt_timeout = 18 if len(resolved_candidates) > 1 else timeout
    for idx, cand in enumerate(resolved_candidates, 1):
        used_proxy = cand
        try:
            if provider == "native":
                url, data, err_msg = _native_checkout(token, cand, workspace, seats, country, currency, promo, per_attempt_timeout, account_proxy)
            else:
                url, data, err_msg = _jinmotech_checkout(token, cand, workspace, seats, country, promo, per_attempt_timeout)
        except Exception as e:
            url, data, err_msg = None, None, f"{type(e).__name__}: {e}"
        if url:
            break
        low_err = (err_msg or "").lower()
        retryable = any(k in low_err for k in ("timeout", "timed out", "connectionerror", "proxy", "cloudflare", "html", "curl: (7)", "curl: (28)", "curl: (35)", "curl: (56)"))
        if retryable and cand:
            try:
                from core.session import invalidate_geo_cache
                invalidate_geo_cache(cand)
            except Exception:  # noqa: BLE001
                pass
        if not retryable or idx >= len(resolved_candidates):
            break
        logger.warning("Team 提链第 %s/%s 个代理出口 (%s) 不可用 (%s)，自动切换下一个出口...", idx, len(resolved_candidates), cand, err_msg[:100])

    meta = {
        "provider": provider,
        "promo_used": promo,
        "promo_from_pool": bool(assigned),
        "proxy_used": used_proxy or "项目代理池",
        "country": country,
        "currency": currency,
        # 命中单席位时席位数是 1，调用方据此判断该备多少钱
        "seats": 1 if (isinstance(data, dict) and data.get("single_seat")) else seats,
        "single_seat": bool(isinstance(data, dict) and data.get("single_seat")),
    }

    if url:
        if assigned:
            # 留在 using，不直接升 used。
            #
            # 2026-11-18 发现：拿到长链 != 码被消耗。支付是后续独立步骤，
            # 失败时 OpenAI 侧并没有扣掉这个码，但池里已经记成 used，
            # 于是一个还能用的码被永久埋掉。那天调试自动订阅就白埋了 3 个
            #（等待确认超时 1 个、金额闸门拦下 2 个）。
            #
            # 现在由 vcc_pay_session._record_promo 在支付真正提交成功后
            # 才升成 used；没付成的留在 using，可由 reclaim_stale_promo_codes
            # 退回 unused。
            mark_promo_code(assigned["id"], "using", account_id, account_email)
        elif promo:
            # 显式传码也要登记。
            #
            # 2026-11-17 线上事故（账号 #83）：指定 TXSFY6YAAG8YMS4J 提链，
            # 码被 OpenAI 真实消耗、账单实打实打了八折（EGP 920 = 1150×0.8），
            # 但池里那条始终挂着 unused——因为显式传码走的是 `if promo` 分支，
            # assigned 恒为 None，上面那句 mark_promo_code 根本不会执行。
            # 后果是下次提链会把这个废码再分派一次，白白失败一轮。
            #
            # 用 record_used_promo_code 而不是 mark_promo_code：前者按 code
            # 查找，池里没有就插入、已存在就补归属，且对已是 used 的记录保留
            # 首次归属，不会把真实使用者改掉。
            try:
                record_used_promo_code(promo, account_id=account_id,
                                       account_email=account_email)
            except Exception:  # noqa: BLE001 - 登记失败不能吃掉已拿到的长链
                logger.exception("显式优惠码登记失败：%s", promo)
        result = {"ok": True, "url": url, "data": data, **meta}
        if auto_pay:
            pay = _maybe_auto_pay(url, account_email or "", used_proxy)
            if pay is not None:
                result["auto_pay"] = pay
        return result

    logger.warning("Team 提链失败 provider=%s account=%s promo=%s proxy=%s: %s",
                   provider, account_id, promo, meta["proxy_used"], err_msg)
    promo_issue = any(k in err_msg.lower() for k in PROMO_ERROR_KEYWORDS)
    if assigned:
        mark_promo_code(assigned["id"], "expired" if promo_issue else "unused", error=err_msg)
    elif promo and promo_issue:
        # 显式传的码被判不可用（not eligible / 已过期），同样要落进池子，
        # 否则它要么不在池里、要么还挂着 unused，下次还会被挑出来再废一轮。
        # 只在确认是"码的问题"时标记：代理超时之类的失败与码无关，标了会冤枉好码。
        try:
            record_used_promo_code(promo, account_id=account_id,
                                   account_email=account_email,
                                   status="expired")
        except Exception:  # noqa: BLE001
            logger.exception("显式优惠码标记失效失败：%s", promo)
    return {"ok": False, "error": err_msg, **meta}
