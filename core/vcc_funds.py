# -*- coding: utf-8 -*-
"""支付前的余额检查与自动充值。

两道关卡：
  1. 账户余额不足 → 在生成长链之前就拦下来，不浪费优惠码
  2. 卡内余额不足 → 从账户划一笔到卡上，等到账再继续

充值走 VCC 的 cards/funds/operate，按 requestNo 幂等。请求号由卡号、
金额和当天日期算出来，同一张卡同一天同样金额重试不会重复扣款——这点
很重要：接口超时或返回 500 时绝不能换新请求号重试。
"""
from __future__ import annotations

import datetime
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class InsufficientFunds(RuntimeError):
    """账户余额不足以完成这次支付。"""


@dataclass
class FundsCheck:
    ok: bool
    card_balance: float = 0.0
    account_balance: float = 0.0
    needed: float = 0.0
    recharged: float = 0.0
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "card_balance": round(self.card_balance, 2),
            "account_balance": round(self.account_balance, 2),
            "needed": round(self.needed, 2),
            "recharged": round(self.recharged, 2),
            "message": self.message,
        }


def _request_no(card_id: str, amount: float) -> str:
    """可复现的充值请求号。

    同一张卡、同一金额、同一天算出同一个号，让接口的幂等机制接住重试。
    换成随机号会导致超时重试时真的扣两笔。
    """
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    seed = f"{card_id}|{amount:.2f}|{day}".encode()
    return "autopay_" + hashlib.sha256(seed).hexdigest()[:24]


def required_usd(cfg: Any) -> float:
    """这次支付大约需要多少 USD。

    账单是 EGP，卡是 USD，按配置的汇率折算并留出余量。汇率写在配置里而
    不是实时查：实时汇率会让"够不够"的判断每次都不一样，排查时说不清。
    """
    amount = float(getattr(cfg, "VCC_EXPECTED_AMOUNT", 0.0) or 0.0)
    rate = float(getattr(cfg, "VCC_USD_RATE", 56.0) or 56.0)
    buffer = float(getattr(cfg, "VCC_BALANCE_BUFFER", 1.0) or 0.0)
    if rate <= 0:
        rate = 56.0
    return round(amount / rate + buffer, 2)


def _recharge_amount(card_balance: float, need: float, top_up: float) -> float:
    """算这次该充多少。

    按配置的整额充（例如 21 USD），不够就充整数倍。不用"刚好补差额"是因为
    那会得到 21.22 这种零碎数字，配置里写的 21 就白写了，对账也难看。
    """
    if top_up <= 0:
        top_up = 21.0
    shortfall = need - card_balance
    if shortfall <= 0:
        return 0.0
    times = 1
    while top_up * times < shortfall:
        times += 1
    return round(top_up * times, 2)


def ensure_funds(client: Any, card_id: str, cfg: Any, *,
                 auto_recharge: bool | None = None) -> FundsCheck:
    """确认卡上钱够付，不够就从账户补。

    账户也不够时抛 InsufficientFunds，调用方应该在生成长链之前就停下。
    """
    from core.vcc_client import VccError

    need = required_usd(cfg)
    if auto_recharge is None:
        auto_recharge = bool(getattr(cfg, "VCC_AUTO_RECHARGE", True))
    top_up = float(getattr(cfg, "VCC_RECHARGE_AMOUNT", 21.0) or 21.0)

    try:
        card_bal = client._available(client.card_balance(card_id))
    except VccError as exc:
        raise InsufficientFunds(f"查询卡余额失败：{exc}") from exc

    if card_bal >= need:
        return FundsCheck(ok=True, card_balance=card_bal, needed=need,
                          message=f"卡内 {card_bal:.2f} USD 足够本次支付（约需 {need:.2f}）")

    if not auto_recharge:
        raise InsufficientFunds(
            f"卡内余额 {card_bal:.2f} USD 不足（约需 {need:.2f}），"
            "且未开启自动充值")

    try:
        acct_bal = client.account_balance()
    except VccError as exc:
        raise InsufficientFunds(f"查询账户余额失败：{exc}") from exc

    # 按配置整额充值，不够则充整数倍
    amount = _recharge_amount(card_bal, need, top_up)
    if acct_bal < amount:
        raise InsufficientFunds(
            f"账户可用余额 {acct_bal:.2f} USD 不足以充值 {amount:.2f} USD"
            f"（卡内 {card_bal:.2f}，本次约需 {need:.2f}）。请先给 VCC 账户充值")

    req_no = _request_no(card_id, amount)
    logger.warning("[funds] 卡 %s 余额 %.2f 不足，从账户充值 %.2f USD（req=%s）",
                   card_id[:8], card_bal, amount, req_no)
    try:
        resp = client.recharge_card(card_id, amount, request_no=req_no)
    except VccError as exc:
        raise InsufficientFunds(f"充值失败：{exc}") from exc

    status = str(resp.get("status") or "").upper()
    if status in ("FAILED", "REJECTED"):
        raise InsufficientFunds(f"充值被拒绝：status={status}")

    # 同步响应只表示受理，要等钱真的到卡上
    timeout = float(getattr(cfg, "VCC_RECHARGE_TIMEOUT", 90.0) or 90.0)
    final = client.wait_card_balance(card_id, need, timeout=timeout)
    if final < need:
        raise InsufficientFunds(
            f"充值已提交但 {int(timeout)} 秒内未到账（当前 {final:.2f}，需 {need:.2f}）。"
            f"请求号 {req_no}，请到 VCC 后台核对，不要重复发起")

    logger.info("[funds] 充值到账，卡余额 %.2f USD", final)
    return FundsCheck(ok=True, card_balance=final, account_balance=acct_bal,
                      needed=need, recharged=amount,
                      message=f"已自动充值 {amount:.2f} USD，卡内现有 {final:.2f} USD")


def preflight(client: Any, card_id: str, cfg: Any) -> dict[str, Any]:
    """只检查不充值，用于生成长链前的拦截判断。"""
    from core.vcc_client import VccError

    need = required_usd(cfg)
    try:
        card_bal = client._available(client.card_balance(card_id))
        acct_bal = client.account_balance()
    except VccError as exc:
        return {"ok": False, "error": f"查询余额失败：{exc}"}

    top_up = float(getattr(cfg, "VCC_RECHARGE_AMOUNT", 21.0) or 21.0)
    if card_bal >= need:
        return {"ok": True, "need_recharge": False, "card_balance": card_bal,
                "account_balance": acct_bal, "needed": need}

    amount = _recharge_amount(card_bal, need, top_up)
    if acct_bal < amount:
        return {
            "ok": False, "need_recharge": True, "card_balance": card_bal,
            "account_balance": acct_bal, "needed": need, "recharge": amount,
            "error": f"卡内 {card_bal:.2f} USD 不足（约需 {need:.2f}），"
                     f"账户可用 {acct_bal:.2f} USD 也不够充值 {amount:.2f} USD。"
                     "请先给 VCC 账户充值再发起支付",
        }
    return {"ok": True, "need_recharge": True, "card_balance": card_bal,
            "account_balance": acct_bal, "needed": need, "recharge": amount}
