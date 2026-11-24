# -*- coding: utf-8 -*-
"""自定义银行卡池。

接口返回的 VCC 卡之外，手头还有一些自己的卡（实体卡、别家发的虚拟卡）。
这些卡没有 API 可查，只能手工登记，支付时和 VCC 卡一起出现在选卡列表里。

和 VCC 卡的区别：
  - 没有 card_id，用本地自增 id，对外统一加 "local:" 前缀避免和 VCC 的撞上
  - 余额是手填的，不会自动刷新，只作参考
  - 卡号/CVV 直接存库。这些本来就是用户自己录进来的，加密存了也要原样
    解出来填表，多一层只是让排查变难；真正该做的是别把库文件泄露出去。
"""
from __future__ import annotations

import datetime
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

DB_PATH = str(Path(__file__).resolve().parents[1] / "turb.sqlite3")

ID_PREFIX = "local:"


class CustomCardError(ValueError):
    """卡信息不合法。"""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    # 读模块属性而不是闭包里的常量，测试才能换到临时库
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_no TEXT NOT NULL,
            exp_month TEXT NOT NULL,
            exp_year TEXT NOT NULL,
            cvv TEXT NOT NULL,
            holder_name TEXT,
            label TEXT,
            balance REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'USD',
            enabled INTEGER NOT NULL DEFAULT 1,
            note TEXT,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_custom_cards_no "
        "ON custom_cards(card_no)"
    )
    conn.commit()
    return conn


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _luhn_ok(number: str) -> bool:
    """Luhn 校验。错一位数字就提交，Stripe 那边只会给个笼统的拒绝。"""
    if not number.isdigit() or len(number) < 12:
        return False
    total, alt = 0, False
    for ch in reversed(number):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def _norm_expiry(month: Any, year: Any) -> tuple[str, str]:
    """统一成 MM / YYYY。年份支持两位，按 20xx 补全。"""
    mm = _digits(month)
    yy = _digits(year)
    if not mm or not yy:
        raise CustomCardError("有效期不能为空")
    if len(mm) > 2 or not (1 <= int(mm) <= 12):
        raise CustomCardError(f"月份不合法：{month}")
    mm = mm.zfill(2)
    if len(yy) == 2:
        yy = "20" + yy
    if len(yy) != 4:
        raise CustomCardError(f"年份不合法：{year}")
    return mm, yy


def _mask(card_no: str) -> str:
    """和 VCC 接口一致的掩码格式，前 6 后 4。"""
    if len(card_no) <= 10:
        return card_no
    return f"{card_no[:6]}****{card_no[-4:]}"


def _row_to_card(row: sqlite3.Row) -> dict:
    """转成和 /api/pay/cards 一致的结构，前端不用区别对待。"""
    d = dict(row)
    no = str(d.get("card_no") or "")
    return {
        "card_id": f"{ID_PREFIX}{d['id']}",
        "local_id": int(d["id"]),
        "mask": _mask(no),
        "balance": float(d.get("balance") or 0.0),
        "status": "ACTIVE" if d.get("enabled") else "DISABLED",
        "holder_id": None,
        "holder_name": str(d.get("holder_name") or ""),
        "card_bin": no[:6],
        "usable": bool(d.get("enabled")),
        "source": "custom",
        "label": str(d.get("label") or ""),
        "currency": str(d.get("currency") or "USD"),
        "note": str(d.get("note") or ""),
        "exp_month": str(d.get("exp_month") or ""),
        "exp_year": str(d.get("exp_year") or ""),
        "use_count": int(d.get("use_count") or 0),
        "last_used_at": d.get("last_used_at"),
        "created_at": d.get("created_at"),
    }


def is_custom_id(card_id: Any) -> bool:
    return str(card_id or "").startswith(ID_PREFIX)


def local_id_of(card_id: Any) -> int:
    """从 "local:12" 取出 12；不是自定义卡返回 0。"""
    s = str(card_id or "")
    if not s.startswith(ID_PREFIX):
        return 0
    try:
        return int(s[len(ID_PREFIX):])
    except ValueError:
        return 0


def add_card(
    card_no: str,
    exp_month: str,
    exp_year: str,
    cvv: str,
    *,
    holder_name: str = "",
    label: str = "",
    balance: float = 0.0,
    currency: str = "USD",
    note: str = "",
) -> dict:
    """登记一张自定义卡。卡号重复时抛错，不静默覆盖。"""
    no = _digits(card_no)
    if not no:
        raise CustomCardError("卡号不能为空")
    if not _luhn_ok(no):
        raise CustomCardError("卡号校验不通过，请检查是否输错")
    mm, yyyy = _norm_expiry(exp_month, exp_year)
    code = _digits(cvv)
    if len(code) not in (3, 4):
        raise CustomCardError("CVV 应为 3 或 4 位数字")
    try:
        bal = float(balance or 0.0)
    except (TypeError, ValueError):
        raise CustomCardError(f"余额不是数字：{balance}") from None

    now = _now()
    with closing(_connect()) as conn, conn:
        exists = conn.execute(
            "SELECT id FROM custom_cards WHERE card_no=?", (no,)).fetchone()
        if exists:
            raise CustomCardError(f"这张卡已经登记过了（{_mask(no)}）")
        cur = conn.execute(
            """
            INSERT INTO custom_cards
                (card_no, exp_month, exp_year, cvv, holder_name, label,
                 balance, currency, enabled, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (no, mm, yyyy, code, str(holder_name or "").strip(),
             str(label or "").strip(), bal, str(currency or "USD").strip().upper(),
             str(note or "").strip(), now, now),
        )
        row = conn.execute("SELECT * FROM custom_cards WHERE id=?",
                           (cur.lastrowid,)).fetchone()
    return _row_to_card(row)


def update_card(local_id: int, **fields: Any) -> dict:
    """改一张卡。只动传进来的字段，卡号不允许改（要改就删了重加）。"""
    allowed = {"exp_month", "exp_year", "cvv", "holder_name", "label",
               "balance", "currency", "enabled", "note"}
    unknown = set(fields) - allowed
    if unknown:
        raise CustomCardError(f"不支持修改这些字段：{sorted(unknown)}")

    sets: list[str] = []
    args: list[Any] = []
    if "exp_month" in fields or "exp_year" in fields:
        with closing(_connect()) as conn:
            cur = conn.execute(
                "SELECT exp_month, exp_year FROM custom_cards WHERE id=?",
                (int(local_id),)).fetchone()
        if cur is None:
            raise CustomCardError(f"卡不存在：{local_id}")
        mm, yyyy = _norm_expiry(fields.get("exp_month", cur["exp_month"]),
                                fields.get("exp_year", cur["exp_year"]))
        sets += ["exp_month=?", "exp_year=?"]
        args += [mm, yyyy]
    if "cvv" in fields:
        code = _digits(fields["cvv"])
        if len(code) not in (3, 4):
            raise CustomCardError("CVV 应为 3 或 4 位数字")
        sets.append("cvv=?")
        args.append(code)
    if "balance" in fields:
        try:
            args.append(float(fields["balance"] or 0.0))
        except (TypeError, ValueError):
            raise CustomCardError(f"余额不是数字：{fields['balance']}") from None
        sets.append("balance=?")
    if "enabled" in fields:
        sets.append("enabled=?")
        args.append(1 if fields["enabled"] else 0)
    for key in ("holder_name", "label", "note"):
        if key in fields:
            sets.append(f"{key}=?")
            args.append(str(fields[key] or "").strip())
    if "currency" in fields:
        sets.append("currency=?")
        args.append(str(fields["currency"] or "USD").strip().upper())

    if not sets:
        raise CustomCardError("没有要改的字段")

    sets.append("updated_at=?")
    args.append(_now())
    args.append(int(local_id))
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            f"UPDATE custom_cards SET {', '.join(sets)} WHERE id=?", args)
        if cur.rowcount == 0:
            raise CustomCardError(f"卡不存在：{local_id}")
        row = conn.execute("SELECT * FROM custom_cards WHERE id=?",
                           (int(local_id),)).fetchone()
    return _row_to_card(row)


def delete_card(local_id: int) -> bool:
    with closing(_connect()) as conn, conn:
        cur = conn.execute("DELETE FROM custom_cards WHERE id=?",
                           (int(local_id),))
        return cur.rowcount > 0


def list_cards(*, only_enabled: bool = False) -> list[dict]:
    """按最近使用排序，常用的排前面。"""
    sql = "SELECT * FROM custom_cards"
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY last_used_at DESC NULLS LAST, use_count DESC, id DESC"
    with closing(_connect()) as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_card(r) for r in rows]


def get_secrets(local_id: int) -> dict[str, str]:
    """取卡号/有效期/CVV，供填表用。只在真正要支付时调。"""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM custom_cards WHERE id=?",
                           (int(local_id),)).fetchone()
    if row is None:
        raise CustomCardError(f"卡不存在：{local_id}")
    return {
        "card_number": str(row["card_no"]),
        "exp_month": str(row["exp_month"]),
        "exp_year": str(row["exp_year"]),
        "cvv": str(row["cvv"]),
        "holder_name": str(row["holder_name"] or ""),
        # 备注名：支付成功后要记到账号上（"这单是哪张卡付的"），
        # 顺手一起返回，省得调用方再查一次卡池。
        "label": str(row["label"] or ""),
    }


def mark_used(local_id: int) -> None:
    """记一次使用，用于排序。失败不抛，别影响支付。"""
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "UPDATE custom_cards SET use_count=use_count+1, "
                "last_used_at=?, updated_at=? WHERE id=?",
                (_now(), _now(), int(local_id)))
    except Exception:  # noqa: BLE001
        pass
