# -*- coding: utf-8 -*-
"""自动订阅调度：按新加坡时间的整点检查并给单席位账号开 Team。

为什么按固定时刻而不是固定间隔：订阅会真实扣款，必须可预期——
人要能知道"下一次扣款可能发生在什么时候"。固定间隔漂移之后就说不清了。

每次只订阅一个账号：
  - 扣款不可逆，一次一个，出问题止损面最小
  - 下一个整点还会再来，不急于一次清空队列
"""
from __future__ import annotations

import datetime
import logging
import threading

logger = logging.getLogger(__name__)

_THREAD: threading.Thread | None = None
_WAKE = threading.Event()
_LOCK = threading.Lock()
_LAST: dict = {"reason": "尚未开始检查"}

# 调度判重用：记下已经处理过的整点，避免同一个点被触发多次
_DONE_SLOT_KEY = "auto_sub_last_slot"

_TZ = datetime.timezone(datetime.timedelta(hours=8))   # 新加坡 UTC+8


def _cfg(name: str, default):
    try:
        from config import auto_subscribe as c
    except Exception:  # noqa: BLE001
        return default
    return getattr(c, name, default)


def _now_sgt() -> datetime.datetime:
    return datetime.datetime.now(_TZ)


def is_enabled() -> bool:
    return bool(_cfg("AUTO_SUBSCRIBE_ENABLED", False))


def _hours() -> list[int]:
    """计划时刻，逗号分隔的字符串解析成整数列表。

    容忍空白和非法值：配置写错一个字段不该让整个调度停摆。
    """
    raw = _cfg("AUTO_SUBSCRIBE_HOURS", "10,12,14,16,18,20")
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = str(raw or "").replace("，", ",").split(",")
    out = []
    for p in parts:
        try:
            v = int(str(p).strip())
        except (TypeError, ValueError):
            continue
        if 0 <= v <= 23:
            out.append(v)
    return sorted(set(out))


def _slot_id(now: datetime.datetime) -> str:
    """当前所属的整点槽位标识，形如 2026-11-18T10。"""
    return now.strftime("%Y-%m-%dT%H")


#: 这些错误意味着"这个账号本身死了"，换下一个候选才有意义。
#: 其余错误（代理超时、支付失败、金额闸门）是环境/流程问题，换个号大概率
#: 还是同样的错，继续试只会白烧额度，应当直接停下等待下一轮。
_DEAD_ACCOUNT_HINTS = (
    "token_revoked",
    "invalidated oauth token",
    "account_deactivated",
    "account_deleted",
    "account_banned",
    # OpenAI 有时返回自然语言而不是 snake_case 错误码
    "account has been deactivated",
    "account was deactivated",
    "account is deactivated",
    "account has been deleted",
)


def _is_dead_account_error(message: str) -> bool:
    """提链/支付失败的原因是不是"账号已死"。

    2026-11-18 实测：#104 的 token 被吊销（token_revoked HTTP 401），但它
    既没查过活、也没被标记，于是稳居候选队首，把 18:00 和 20:00 两个整点
    全部撞废——后面的活号一次都没轮到。
    """
    low = str(message or "").lower()
    return any(hint in low for hint in _DEAD_ACCOUNT_HINTS)


def _mark_account_dead(account_id: int, reason: str) -> None:
    """把确认死亡的账号标记为 deactivated，让它不再进入候选队列。

    只写 live_check_status / live_check_error 两个字段：这是系统里统一的
    "废号"口径，账号列表筛选、删废号、查活跳过都认它，不需要新增字段。
    标记失败不影响订阅流程本身。
    """
    try:
        from core import db
        db.update_account_liveness(
            int(account_id),
            {"ok": False, "status": "deactivated", "error": reason or "token_revoked"},
        )
        logger.warning("[自动订阅] 账号 #%s 已标记为废号并移出候选：%s",
                       account_id, reason or "token_revoked")
    except Exception:  # noqa: BLE001 - 标记失败不该影响主流程
        logger.warning("[自动订阅] 标记废号失败：账号 #%s", account_id, exc_info=True)


def next_run_at(now: datetime.datetime | None = None) -> datetime.datetime | None:
    """下一个触发时刻（新加坡时间）。"""
    now = now or _now_sgt()
    hours = _hours()
    if not hours:
        return None
    for h in hours:
        cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand > now:
            return cand
    nxt = now + datetime.timedelta(days=1)
    return nxt.replace(hour=hours[0], minute=0, second=0, microsecond=0)


def find_candidates(limit: int = 50) -> list[dict]:
    """找出可以自动订阅的账号。

    条件（全部满足）：
      - 支持单席位（single_seat_supported=True）——只订一席，省一半
      - 还没订阅（没有 paid_at / subscription_no，套餐仍是 free）
      - 没归档、有 access_token
      - 没被标记为废号

    排序：id 升序，先建先订，避免新号插队把老号饿死。
    """
    from core import db

    out = []
    try:
        # list_accounts 返回 list[dict]；list_accounts_page 才返回 {items:...}。
        # 两种都兼容，免得以后换接口又踩一次。
        raw = db.list_accounts(limit=5000, archived="0")
        rows = raw.get("items") if isinstance(raw, dict) else (raw or [])
    except Exception:  # noqa: BLE001
        logger.exception("[自动订阅] 读取账号失败")
        return out

    for r in rows:
        if r.get("single_seat_supported") is not True:
            continue
        if r.get("paid_at") or r.get("subscription_no"):
            continue
        plan = str(r.get("current_plan_type") or r.get("plan_type") or "").lower()
        if plan and plan != "free":
            continue
        if str(r.get("live_check_status") or "") == "deactivated":
            continue
        # 查活已经明确读过"账号已废/token 已吊销"的，别再拿来试。
        # 只看 live_check_error 里的死号特征，不看 live_check_ok=False——
        # 那条多为"OTP 超时/网络失败"，账号其实可能是活的。
        if _is_dead_account_error(str(r.get("live_check_error") or "")):
            continue
        if not str(r.get("access_token") or "").strip():
            continue
        out.append(r)
        if len(out) >= limit:
            break
    out.sort(key=lambda r: int(r.get("id") or 0))
    return out


def _busy() -> str:
    """有支付会话在跑就别再起一个，返回非空表示忙。"""
    try:
        from core.vcc_pay_session import list_active
        sess = list_active()
        if sess:
            return "已有支付会话在进行（%s）" % getattr(sess, "sid", "?")
    except Exception:  # noqa: BLE001
        logger.debug("[自动订阅] 查支付会话失败", exc_info=True)
    return ""


def run_once(*, force: bool = False) -> dict:
    """执行一次检查；满足条件就给一个账号发起订阅。

    force=True 时忽略整点判断，用于手动触发和联调。
    """
    now = _now_sgt()
    slot = _slot_id(now)

    if not is_enabled():
        return {"ok": False, "acted": False, "reason": "自动订阅未启用"}

    if not force:
        if now.hour not in _hours():
            return {"ok": True, "acted": False,
                    "reason": "当前 %02d 点不在计划内" % now.hour}
        from core import db
        if str(db.get_meta(_DONE_SLOT_KEY) or "") == slot:
            return {"ok": True, "acted": False, "reason": "本整点已处理过"}

    busy = _busy()
    if busy:
        return {"ok": True, "acted": False, "reason": busy}

    cands = find_candidates()
    if not cands:
        if not force:
            from core import db
            db.set_meta(_DONE_SLOT_KEY, slot)
        return {"ok": True, "acted": False, "reason": "没有待订阅的单席位账号"}

    # 按顺序逐个尝试：遇到死号就标记为废号并换下一个，其余错误直接停。
    #
    # 旧逻辑只试 cands[0]，失败就放弃且立刻占用整点。#104 那种"token 被吊销
    # 但没被标记"的号会稳居队首，让每个整点都在它身上撞死（2026-11-18 的
    # 18:00 和 20:00 就这么废掉的）。现在死号会被踢出队列，后面的活号能轮到。
    tried: list[dict] = []
    last_res: dict = {}
    for idx, acc in enumerate(cands):
        acc_id = int(acc.get("id") or 0)
        email = str(acc.get("email") or "")

        logger.warning("[自动订阅] 开始为账号 #%s %s 订阅 Team（候选 %d 个，第 %d 个）",
                       acc_id, email, len(cands), idx + 1)
        try:
            res = start_subscription(acc_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[自动订阅] 发起失败：账号 #%s", acc_id)
            res = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

        message = str(res.get("message") or res.get("error") or "")
        tried.append({"account_id": acc_id, "email": email, "ok": bool(res.get("ok")),
                      "reason": message})
        last_res = res

        if res.get("ok"):
            if not force:
                from core import db
                db.set_meta(_DONE_SLOT_KEY, slot)
            return {"ok": True, "acted": True, "account_id": acc_id, "email": email,
                    "reason": message, "session_id": res.get("session_id") or "",
                    "tried": tried}

        if _is_dead_account_error(message):
            _mark_account_dead(acc_id, message)
            continue

        # 非死号错误：换号大概率同样失败，停下等下一轮，且不占用整点，
        # 让下一次调度仍有机会重试。
        logger.error("[自动订阅] 账号 #%s 订阅失败（非死号错误，本轮停止）：%s",
                     acc_id, message)
        return {"ok": False, "acted": True, "account_id": acc_id, "email": email,
                "reason": message, "tried": tried}

    # 所有候选都试过且都是死号：占用整点，避免本小时内反复空转。
    if not force:
        from core import db
        db.set_meta(_DONE_SLOT_KEY, slot)
    logger.error("[自动订阅] 全部 %d 个候选账号都已失效，本轮无可用账号", len(cands))
    return {"ok": False, "acted": False,
            "reason": "候选账号全部失效（均为死号）", "tried": tried}


def _card_blacklist() -> set[str]:
    """不参与随机选卡的卡号后四位。

    某张卡被风控标记、留作他用、或余额是给别的流程备的，都不该被
    自动订阅抽到。
    """
    raw = _cfg("AUTO_SUBSCRIBE_CARD_BLACKLIST", "")
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = str(raw or "").replace("，", ",").split(",")
    out = set()
    for p in parts:
        s = "".join(ch for ch in str(p) if ch.isdigit())
        if s:
            out.add(s[-4:])
    return out


_CARD_USAGE_KEY = "auto_sub_card_usage"


def card_usage() -> dict[str, int]:
    """每张卡的使用次数，按卡号后四位计。

    存在 storage_meta 里而不是新建表：这是个很小的计数，加表要迁移、
    要备份，不划算。VCC 托管卡来自远端 API，本地也没有卡表可挂。
    """
    import json

    from core import db
    try:
        raw = db.get_meta(_CARD_USAGE_KEY) or "{}"
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - 计数坏了不该让订阅停摆
        logger.warning("[自动订阅] 卡使用次数读取失败，按全 0 处理", exc_info=True)
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


def note_card_used(card_mask: str) -> None:
    """记一次使用。支付真正发起后才调，没发起的不算。"""
    import json

    from core import db
    tail = "".join(ch for ch in str(card_mask or "") if ch.isdigit())[-4:]
    if not tail:
        return
    try:
        data = card_usage()
        data[tail] = data.get(tail, 0) + 1
        db.set_meta(_CARD_USAGE_KEY, json.dumps(data, ensure_ascii=False))
        logger.info("[自动订阅] 卡 %s 使用次数 -> %d", tail, data[tail])
    except Exception:  # noqa: BLE001 - 记不上不影响已发起的支付
        logger.warning("[自动订阅] 记录卡使用次数失败", exc_info=True)


def _pick_balanced(cards: list[dict]) -> dict:
    """从使用次数最少的那一档里随机选一张。

    为什么不是纯随机：纯随机在样本少时会严重偏科，某张卡连用五次而
    另一张一次没用。先按次数分档、只在最低档里随机，既保证均衡，
    又不会变成固定轮转（固定顺序容易被风控识别成规律）。
    """
    import random

    usage = card_usage()

    def times(c) -> int:
        mask = str(c.get("cardNoMask") or "")
        tail = "".join(ch for ch in mask if ch.isdigit())[-4:]
        return usage.get(tail, 0)

    fewest = min(times(c) for c in cards)
    pool = [c for c in cards if times(c) == fewest]
    pick = random.choice(pool)
    logger.info("[自动订阅] 均衡选卡：最少用过 %d 次的有 %d 张，选中 %s",
                fewest, len(pool), pick.get("cardNoMask"))
    return pick


def _addr_is_payable(client, card: dict) -> bool:
    """这张卡的账单地址能不能用来支付。

    城市和州对不上的地址（如 "Philadelphia, DE"）会被 Stripe 按真实辖区
    收税，920 的八折单变成 993.60，必然撞上金额闸门。在选卡阶段就筛掉，
    比跑到付款页才被拦下省事得多——那一轮要几分钟，还白占一个长链。
    """
    from core.card_address import CardAddressError, fetch_card_address
    mask = str(card.get("cardNoMask") or "")
    try:
        sec = client.card_secrets(str(card.get("cardId") or ""))
        addr = fetch_card_address(sec.get("number") or "")
    except CardAddressError as exc:
        logger.warning("[自动订阅] 卡 %s 取地址失败，跳过：%s", mask, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[自动订阅] 卡 %s 取地址异常，跳过：%s: %s",
                       mask, type(exc).__name__, exc)
        return False
    if addr.get("state_mismatch"):
        logger.warning("[自动订阅] 卡 %s 地址城市/州不匹配（%s, %s），"
                       "会被多收税，跳过", mask, addr["city"], addr["state"])
        return False
    return True


def start_subscription(account_id: int) -> dict:
    """给一个账号走完整订阅流程。

    步骤和人在界面上点「订阅Team」一致：
      1. 随机选一张余额够付的 VCC 卡
      2. 卡内余额不足时自动充值
      3. 按结算国家取 1024proxy 出口（出口国家必须和结算货币同区）
      4. 生成单席位 Team 长链（优惠码从池里自动分派）
      5. 起支付会话；账单地址在会话内按卡号实时查

    返回 {ok, session_id, card, error}。
    """
    import random

    from core import db
    from core.vcc_client import build_client_from_config
    from core.vcc_funds import InsufficientFunds, ensure_funds, required_usd

    cfg = _vcc_cfg()
    acc = db.get_account(int(account_id))
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    token = str(acc.get("access_token") or "").strip()
    if not token:
        return {"ok": False, "error": "账号没有 access_token"}

    # ---- 选卡：黑名单之外随机挑一张，余额不足由 ensure_funds 自动充值 ----
    #
    # 不按余额筛卡：卡内不够时会从 VCC 账户划钱过来（和手动订阅同一套逻辑）。
    # 按余额筛会让刚用过的卡长期选不中，失去"分散单卡用量"的意义。
    client = build_client_from_config()
    need = required_usd(cfg)
    blacklist = _card_blacklist()
    usable = []
    skipped_black = []
    for c in client.list_cards(page_size=100) or []:
        if str(c.get("cardStatus") or "").upper() != "ACTIVE":
            continue
        mask = str(c.get("cardNoMask") or "")
        tail = "".join(ch for ch in mask if ch.isdigit())[-4:]
        if tail and tail in blacklist:
            skipped_black.append(tail)
            continue
        usable.append(c)
    if skipped_black:
        logger.info("[自动订阅] 黑名单跳过 %d 张卡：%s",
                    len(skipped_black), ",".join(sorted(set(skipped_black))))
    if not usable:
        return {"ok": False,
                "error": "没有可用的 VCC 卡（黑名单跳过 %d 张）" % len(skipped_black)}

    pick = _pick_balanced(usable)
    card_id = str(pick.get("cardId") or "")
    card_mask = str(pick.get("cardNoMask") or "")

    # 地址不可用就换一张：按使用次数依次往下试，别卡在同一张坏卡上
    if not _addr_is_payable(client, pick):
        rest = [c for c in usable if c is not pick]
        for _ in range(len(rest)):
            pick = _pick_balanced(rest)
            if _addr_is_payable(client, pick):
                break
            rest = [c for c in rest if c is not pick]
        else:
            return {"ok": False,
                    "error": "所有卡的账单地址都不可用（城市/州不匹配或取不到）"}
        card_id = str(pick.get("cardId") or "")
        card_mask = str(pick.get("cardNoMask") or "")

    logger.info("[自动订阅] 选中卡 %s（余额 %s，候选 %d 张，需 %.2f USD）",
                card_mask, pick.get("balance"), len(usable), need)

    # ---- 余额兜底：不够就从账户充 ----
    try:
        check = ensure_funds(client, card_id, cfg)
        if check.recharged:
            logger.warning("[自动订阅] 已自动充值 %.2f USD 到卡 %s",
                           check.recharged, card_mask)
    except InsufficientFunds as exc:
        return {"ok": False, "error": "余额不足：%s" % exc}

    # ---- 出口：结算国家和出口国家必须同区，否则 Stripe 拒付 ----
    country = str(_cfg("AUTO_SUBSCRIBE_COUNTRY", "US") or "US").upper()
    from core.team_checkout_service import COUNTRY_CURRENCY
    currency = COUNTRY_CURRENCY.get(country, "")
    if not currency:
        return {"ok": False, "error": "国家 %s 没有内置货币映射" % country}

    pay_proxy = ""
    try:
        from webui.app import _first_proxy_in_country
        from core.team_checkout_service import fetch_1024proxy_ips
        ips = fetch_1024proxy_ips(
            country, 4, minutes=int(getattr(cfg, "VCC_1024PROXY_MINUTES", 60)))
        pay_proxy = _first_proxy_in_country(ips, country)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[自动订阅] 取 %s 出口失败：%s", country, exc)
    if not pay_proxy:
        return {"ok": False,
                "error": "拿不到 %s 出口，无法保证结算国家一致" % country}

    # ---- 生成长链（单席位）----
    from core.team_checkout_service import generate_team_checkout_link
    res = generate_team_checkout_link(
        token=token,
        proxy_address=pay_proxy,
        workspace=str(_cfg("AUTO_SUBSCRIBE_WORKSPACE", "MyTeam")),
        seats=2,                 # 服务端支持时会自动降到 1 席
        country=country,
        currency=currency,
        use_1024proxy=False,     # 已显式给出口，别再让它自己取
        account_id=int(account_id),
        account_email=acc.get("email"),
        account_proxy=str(acc.get("proxy_used") or ""),
        # 提链阶段不要顺手付掉：单席位结账页必须有登录态才打得开，
        # 而提链内置的支付是裸 pay_checkout、不带 cookie，打开只会落到
        # /auth/login 并把这张链白烧掉。支付统一交给下面的 pay-session，
        # 它会先真登录、注入 session-token 再填表。
        auto_pay=False,
    )
    if not res.get("ok"):
        return {"ok": False, "error": "生成长链失败：%s" % res.get("error")}

    url = str(res.get("url") or "")
    seats = res.get("seats")
    if seats and int(seats) > 1:
        return {"ok": False,
                "error": "该账号本次只拿到 %s 席，不符合单席位条件，已放弃" % seats}
    db.update_account_team_checkout(int(account_id), {"url": url})
    logger.info("[自动订阅] 长链已生成（%s 席，%s/%s）：账号 #%s",
                seats or "?", country, currency, account_id)

    # ---- 起支付会话；账单地址在会话内按卡号查 ----
    #
    # 金额闸门按结算货币计（EG 区就是 EGP）。备款额 VCC_FUNDING_USD=18
    # 是另一个口径——那是给卡充钱用的 USD，920 EGP ≈ 17.17 USD，两者对得上。
    run_cfg = _with_card(cfg, card_id)
    cap = float(_cfg("AUTO_SUBSCRIBE_MAX_AMOUNT", 0.0) or 0.0)
    if cap > 0:
        run_cfg.VCC_MAX_CHARGE = cap
        run_cfg.VCC_EXPECTED_CURRENCY = currency
        logger.info("[自动订阅] 金额闸门：最多 %s %.2f", currency, cap)
        # 闸门把住了金额和币种，就不必再等人点确认——自动流程没人盯着，
        # 停在"等待确认"只会让会话超时回收，整单白跑。
        # 闸门没过时 _wait_and_submit 仍然会停下等人，不会盲目提交。
        run_cfg.VCC_AUTO_CONFIRM = True
    else:
        # 没设上限就不敢自动付：那等于没有任何金额校验
        run_cfg.VCC_AUTO_CONFIRM = False
        logger.warning("[自动订阅] 未设金额上限，将停在等待确认")

    from core.vcc_pay_session import start_session
    sess = start_session(
        account_id=int(account_id),
        email=str(acc.get("email") or ""),
        checkout_url=url,
        proxy=pay_proxy,
        cfg=run_cfg,
        promo_code=str(res.get("promo_used") or ""),
    )
    # 会话真起来了才记一次使用：前面任何一步失败都不该占用配额，
    # 否则失败几次就会把一张好卡挤到"用得最多"那一档去。
    note_card_used(card_mask)
    return {"ok": True, "session_id": getattr(sess, "sid", ""),
            "card": card_mask, "seats": seats,
            "message": "已发起支付会话（卡 %s，上限 %s %.2f）"
                       % (card_mask, currency, cap) if cap > 0
                       else "已发起支付会话（卡 %s）" % card_mask}


def _with_card(cfg, card_id: str):
    """复制一份配置并钉住本次要用的卡。

    不能直接改全局 cfg：那会影响其他正在跑的流程。
    """
    import copy
    import types

    clone = types.SimpleNamespace()
    for key in dir(cfg):
        if key.isupper():
            try:
                setattr(clone, key, copy.copy(getattr(cfg, key)))
            except Exception:  # noqa: BLE001
                setattr(clone, key, getattr(cfg, key))
    clone.VCC_CARD_ID = card_id
    return clone


def _vcc_cfg():
    from config import vcc
    return vcc


def status() -> dict:
    nxt = next_run_at()
    with _LOCK:
        last = dict(_LAST)
    try:
        n = len(find_candidates())
    except Exception:  # noqa: BLE001
        n = -1
    return {
        "enabled": is_enabled(),
        "hours": _hours(),
        "timezone": "Asia/Singapore (UTC+8)",
        "next_run_at": nxt.isoformat(timespec="seconds") if nxt else None,
        "candidates": n,
        "card_usage": card_usage(),
        "card_blacklist": sorted(_card_blacklist()),
        "running": _THREAD is not None and _THREAD.is_alive(),
        "last_action": last,
    }


def _loop() -> None:
    logger.info("[自动订阅] 调度线程已启动，计划时刻（新加坡）：%s", _hours())
    while True:
        # 每分钟醒一次就够：整点判断精确到小时
        _WAKE.wait(timeout=float(_cfg("AUTO_SUBSCRIBE_POLL_INTERVAL", 60.0)))
        _WAKE.clear()
        try:
            res = run_once()
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "acted": False,
                   "reason": "%s: %s" % (type(exc).__name__, exc)}
            logger.exception("[自动订阅] 调度循环异常")
        res["checked_at"] = _now_sgt().isoformat(timespec="seconds")
        with _LOCK:
            global _LAST
            _LAST = res


def start() -> None:
    """启动调度线程；重复调用无副作用。"""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_loop, name="auto-subscribe",
                                   daemon=True)
        _THREAD.start()
