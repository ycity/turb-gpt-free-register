# -*- coding: utf-8 -*-
"""AiLink VCC 开放 API 客户端。

签名原文（文档 "签名原文" 一节）：
    HTTP_METHOD + "\\n" + REQUEST_PATH + "\\n" + TIMESTAMP + "\\n"
    + NONCE + "\\n" + SHA256_HEX(RAW_REQUEST_BODY)
HMAC-SHA256，密钥为 API Secret。

卡号/有效期/CVV 以 base64url(iv + ciphertext + tag) 返回，AES-256-GCM，
密钥 = HMAC-SHA256(webhookSecret, "vcc-webhook-sensitive-v1")。注意是
Webhook Secret 而非 API Secret。

本模块只做查询与解密，不含开卡、充值、销卡等动账操作。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SENSITIVE_KEY_INFO = b"vcc-webhook-sensitive-v1"


class VccError(RuntimeError):
    """VCC 接口调用失败。"""

    def __init__(self, message: str, *, code: Any = None, request_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


def _b64url_decode(value: str) -> bytes:
    """解码不带 = 填充的 base64url。"""
    text = value.strip().replace("-", "+").replace("_", "/")
    pad = (-len(text)) % 4
    return base64.b64decode(text + "=" * pad)


def decrypt_sensitive(ciphertext: str, webhook_secret: str) -> str:
    """解密卡号/有效期/CVV 密文。"""
    if not ciphertext:
        return ""
    if not webhook_secret:
        raise VccError("缺少 Webhook Secret，无法解密卡片三要素")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = hmac.new(webhook_secret.encode(), SENSITIVE_KEY_INFO, hashlib.sha256).digest()
    blob = _b64url_decode(ciphertext)
    if len(blob) < 12 + 16:
        raise VccError("卡片密文长度异常")
    try:
        plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)
    except Exception as exc:  # noqa: BLE001 - 统一成业务错误，避免泄露密文
        raise VccError(f"卡片三要素解密失败：{type(exc).__name__}") from exc
    return plain.decode("utf-8", "strict")


def load_credentials_file(path: str | Path) -> dict[str, str]:
    """读取 `key: xxx` / `key=xxx` 形式的凭据文件。"""
    out: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in (":", "="):
            if sep in line:
                name, value = line.split(sep, 1)
                out[name.strip().lower()] = value.strip()
                break
    return out


def _normalize_expiry(raw: str) -> tuple[str, str]:
    """把有效期归一成 (MM, YY)。

    上游可能返回 MM/YY、MM/YYYY、MMYY、YYYY-MM 等写法。
    """
    text = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not text:
        return "", ""
    if len(text) == 4:                      # MMYY
        return text[:2], text[2:]
    if len(text) == 6:
        head, tail = text[:2], text[2:]
        if head in {"20", "19"}:            # YYYYMM
            return text[4:], text[2:4]
        return head, tail[2:]               # MMYYYY
    if len(text) >= 6 and raw.find("-") in (4, 5):   # YYYY-MM
        return text[4:6], text[2:4]
    return text[:2], text[-2:]


class VccClient:
    """VCC 开放 API 只读客户端。"""

    def __init__(self, api_key: str, api_secret: str, *,
                 webhook_secret: str = "",
                 base_url: str = "https://card.ailinkvcc.com",
                 timeout: float = 30.0) -> None:
        if not api_key or not api_secret:
            raise VccError("缺少 VCC API Key / Secret")
        self.api_key = api_key
        self.api_secret = api_secret
        self.webhook_secret = webhook_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---- 底层 ----

    def _sign(self, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
        prehash = "\n".join([method, path, ts, nonce, hashlib.sha256(body).hexdigest()])
        return hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()

    def post(self, path: str, payload: dict | None = None) -> dict:
        """调用一个开放接口并返回 data。"""
        body = json.dumps(payload or {}, separators=(",", ":"), ensure_ascii=False).encode()
        ts = str(int(time.time() * 1000))
        nonce = secrets.token_hex(12)
        req = urllib.request.Request(
            self.base_url + path, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-VCC-API-KEY": self.api_key,
                "X-VCC-TIMESTAMP": ts,
                "X-VCC-NONCE": nonce,
                "X-VCC-SIGNATURE": self._sign("POST", path, ts, nonce, body),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except Exception as exc:  # noqa: BLE001
            raise VccError(f"VCC 请求失败：{type(exc).__name__}: {exc}") from exc

        try:
            doc = json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            raise VccError(f"VCC 返回非 JSON（HTTP {status}）") from exc

        code = doc.get("code")
        request_id = str(doc.get("requestId") or "")
        if code != 200:
            msg = str(doc.get("msg") or "").strip() or f"HTTP {status}"
            hint = ""
            if code == 403:
                hint = "（来源 IP 不在白名单或签名校验失败）"
            elif code == 401:
                hint = "（API Key 无效或未开启 API 权限）"
            raise VccError(f"{path} 调用失败：code={code} {msg}{hint}",
                           code=code, request_id=request_id)
        data = doc.get("data")
        return data if isinstance(data, dict) else {"_raw": data}

    @staticmethod
    def _rows(data: dict) -> list[dict]:
        for key in ("list", "items", "records", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        return []

    # ---- 查询 ----

    def list_cardholders(self, *, page_size: int = 50) -> list[dict]:
        return self._rows(self.post("/openapi/v1/cardholders/query",
                                    {"pageNum": 1, "pageSize": page_size}))

    def pick_cardholder(self, holder_id: int = 0) -> dict:
        """选定一个持卡人。

        注意：cardholders/query 只返回姓名/邮箱/国家等，不返回
        state / city / address / postCode —— 账单地址需由配置提供。
        """
        holders = self.list_cardholders()
        if not holders:
            raise VccError("VCC 未查询到任何持卡人")
        if holder_id:
            for row in holders:
                if str(row.get("holderId")) == str(holder_id):
                    return row
            raise VccError(f"未找到 holderId={holder_id} 的持卡人")
        for row in holders:
            if row.get("merchantDefault"):
                return row
        active = [r for r in holders if str(r.get("holderStatus", "")).upper() == "ACTIVE"]
        if len(active) == 1:
            return active[0]
        ids = [r.get("holderId") for r in (active or holders)]
        raise VccError(
            f"存在 {len(ids)} 个可用持卡人且无 merchantDefault，请用 VCC_HOLDER_ID 指定：{ids}")

    def list_cards(self, *, page_size: int = 50, card_id: str = "") -> list[dict]:
        payload: dict[str, Any] = {"pageNum": 1, "pageSize": page_size}
        if card_id:
            payload["cardId"] = card_id
        return self._rows(self.post("/openapi/v1/cards/query", payload))

    def card_balance(self, card_id: str) -> dict:
        data = self.post("/openapi/v1/cards/balances/query", {"cardIds": [card_id]})
        for row in self._rows(data):
            if str(row.get("cardId")) == str(card_id):
                return row
        return data

    def account_balance(self, currency: str = "USD") -> float:
        """账户可用余额。充值卡片的钱从这里出。"""
        data = self.post("/openapi/v1/account/balance/query", {})
        for row in data.get("balances") or []:
            if str(row.get("currency") or "").upper() == currency.upper():
                return self._available(row)
        return 0.0

    def recharge_card(self, card_id: str, amount: float, *,
                      request_no: str, currency: str = "USD") -> dict:
        """给卡充值。真实划走账户余额。

        request_no 必须由调用方给定且可复现：接口按它做幂等，重复提交同一个
        请求号不会重复扣款。超时或 500 时绝不能换新请求号重试——那会变成
        两笔真实划账。

        返回里 status 首次通常是 PROCESSING，几秒后才到卡上。
        """
        if amount <= 0:
            raise VccError(f"充值金额必须为正数，收到 {amount}")
        if not request_no:
            raise VccError("充值必须提供 request_no，否则重试会重复扣款")
        return self.post("/openapi/v1/cards/funds/operate", {
            "cardId": card_id,
            "operationType": "RECHARGE",
            "amount": f"{amount:.2f}",
            "currency": currency,
            "requestNo": request_no,
        })

    def wait_card_balance(self, card_id: str, target: float, *,
                          timeout: float = 60.0, interval: float = 3.0) -> float:
        """等卡余额涨到目标值。充值是异步的，同步响应只表示受理。"""
        deadline = time.time() + timeout
        last = 0.0
        while time.time() < deadline:
            last = self._available(self.card_balance(card_id))
            if last >= target:
                return last
            time.sleep(interval)
        return last

    @staticmethod
    def _is_active(card: dict) -> bool:
        """卡状态是否可用。接口返回字符串 'ACTIVE'，老文档写的是数字 1，两种都认。"""
        status = card.get("cardStatus")
        if isinstance(status, str):
            return status.strip().upper() == "ACTIVE"
        try:
            return int(status or 0) == 1
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _available(row: dict) -> float:
        """从余额行里取可用额度。实测返回的是 balance 字段。"""
        for name in ("availableBalance", "availableAmount", "balance"):
            value = row.get(name)
            if value in (None, ""):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    def pick_card(self, *, card_id: str = "", min_balance: float = 0.0) -> dict:
        """选一张可用卡。"""
        cards = self.list_cards(card_id=card_id)
        if not cards:
            raise VccError(f"VCC 未查询到可用卡{f'（cardId={card_id}）' if card_id else ''}")
        if card_id:
            card = cards[0]
            if not self._is_active(card):
                raise VccError(f"卡 {card_id} 状态不可用：cardStatus={card.get('cardStatus')}")
            return card

        usable = [c for c in cards if self._is_active(c)]
        if not usable:
            raise VccError("所有卡状态均不可用")
        if min_balance <= 0:
            return usable[0]
        for card in usable:
            cid = str(card.get("cardId") or "")
            # 卡行自带 balance，优先用它，省一次请求
            avail = self._available(card)
            if not avail:
                try:
                    avail = self._available(self.card_balance(cid))
                except VccError:
                    logger.debug("[vcc] 卡 %s 余额查询失败，跳过该卡", cid)
                    continue
            if avail >= min_balance:
                return card
        raise VccError(f"没有可用余额 >= {min_balance} 的卡")

    def card_secrets(self, card_id: str) -> dict[str, str]:
        """取卡号/有效期/CVV 明文。返回值含敏感信息，禁止写日志。"""
        data = self.post("/openapi/v1/cards/private-info/query", {"cardId": card_id})
        rows = self._rows(data)
        info = rows[0] if rows else data

        def pick(*names: str) -> str:
            for name in names:
                value = info.get(name)
                if isinstance(value, str) and value:
                    return value
            return ""

        number_ct = pick("cardNumberCiphertext", "cardNoCiphertext")
        expiry_ct = pick("expiryDateCiphertext", "expiryCiphertext")
        cvv_ct = pick("cvvCiphertext", "cvcCiphertext")
        if not (number_ct and expiry_ct and cvv_ct):
            missing = [n for n, v in (("卡号", number_ct), ("有效期", expiry_ct), ("CVV", cvv_ct)) if not v]
            raise VccError(f"private-info 未返回密文字段：{'/'.join(missing)}")

        number = decrypt_sensitive(number_ct, self.webhook_secret)
        expiry = decrypt_sensitive(expiry_ct, self.webhook_secret)
        cvv = decrypt_sensitive(cvv_ct, self.webhook_secret)
        month, year = _normalize_expiry(expiry)
        if not (month and year):
            raise VccError("卡有效期解析失败")
        return {
            "number": "".join(ch for ch in number if ch.isdigit()),
            "exp_month": month,
            "exp_year": year,
            "cvc": cvv.strip(),
        }

    def fetch_3ds_code(self, card_id: str, *, since: float = 0.0) -> str:
        """取一次 3DS 验证码；未到则返回空串。"""
        data = self.post("/openapi/v1/cards/verification-code/query", {"cardId": card_id})
        rows = self._rows(data) or ([data] if data.get("otpCode") else [])
        best_code, best_ts = "", since
        for row in rows:
            code = str(row.get("otpCode") or row.get("code") or "").strip()
            if not code:
                continue
            raw_ts = str(row.get("transactionTime") or row.get("receivedAt") or "").strip()
            ts = 0.0
            if raw_ts:
                try:
                    ts = time.mktime(time.strptime(raw_ts, "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    ts = 0.0
            if ts >= best_ts:
                best_code, best_ts = code, ts
        return best_code

    def wait_3ds_code(self, card_id: str, *, timeout: float = 180.0,
                      interval: float = 5.0, since: float = 0.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                code = self.fetch_3ds_code(card_id, since=since)
            except VccError as exc:
                logger.debug("[vcc] 3DS 轮询失败：%s", exc)
                code = ""
            if code:
                return code
            time.sleep(interval)
        return ""


def build_client_from_config() -> VccClient:
    """按 config/vcc.py 构建客户端，凭据可来自配置或凭据文件。"""
    from config import vcc as cfg

    key = (cfg.VCC_API_KEY or "").strip()
    secret = (cfg.VCC_API_SECRET or "").strip()
    webhook = (cfg.VCC_WEBHOOK_SECRET or "").strip()
    cred_file = (cfg.VCC_CRED_FILE or "").strip()
    if cred_file and not (key and secret and webhook):
        try:
            loaded = load_credentials_file(cred_file)
        except OSError as exc:
            raise VccError(f"读取 VCC 凭据文件失败：{exc}") from exc
        key = key or loaded.get("key", "")
        secret = secret or loaded.get("secret", "")
        webhook = webhook or loaded.get("webhook_secret", "")
    return VccClient(key, secret, webhook_secret=webhook,
                     base_url=cfg.VCC_API_BASE, timeout=cfg.VCC_API_TIMEOUT)
