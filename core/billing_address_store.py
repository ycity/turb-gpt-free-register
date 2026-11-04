# -*- coding: utf-8 -*-
"""账单地址簿。

自动支付弹窗里选过的账单地址存在这里，下次可以直接下拉选，不用重填。
地址本身不敏感（是卡登记的真实地址），但和卡的对应关系有价值，所以按
"最近使用"排序，常用的排在前面。
"""
from __future__ import annotations

import datetime
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

DB_PATH = str(Path(__file__).resolve().parents[1] / "turb.sqlite3")

FIELDS = ("country", "state", "city", "line1", "line2", "postal", "name")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    # 读模块属性而不是闭包里的常量，测试才能换到临时库
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_addresses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            name TEXT,
            country TEXT NOT NULL,
            state TEXT,
            city TEXT NOT NULL,
            line1 TEXT NOT NULL,
            line2 TEXT,
            postal TEXT,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_billing_last_used "
        "ON billing_addresses(last_used_at DESC)")
    # 卡 ↔ 地址的使用关系。同一张卡换过地址时保留多条，按 last_used_at 取最近的。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_address_cards (
            card_id TEXT NOT NULL,
            address_id INTEGER NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (card_id, address_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_billing_card_recent "
        "ON billing_address_cards(card_id, last_used_at DESC)")
    conn.commit()
    return conn


def _fingerprint(addr: dict[str, Any]) -> str:
    """同一个地址只存一条，大小写和空格不计。"""
    parts = [str(addr.get(f) or "").strip().lower() for f in FIELDS]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _label(addr: dict[str, Any]) -> str:
    bits = [str(addr.get(k) or "").strip()
            for k in ("line1", "city", "state", "postal", "country")]
    return ", ".join(b for b in bits if b)


def normalize(addr: dict[str, Any]) -> dict[str, str]:
    out = {f: str(addr.get(f) or "").strip() for f in FIELDS}
    out["country"] = out["country"].upper()
    return out


def list_addresses(limit: int = 50) -> list[dict]:
    """按最近使用排序；没用过的按创建时间兜底。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM billing_addresses "
            "ORDER BY COALESCE(last_used_at, created_at) DESC, id DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_address(addr_id: int) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM billing_addresses WHERE id=?",
                           (int(addr_id),)).fetchone()
    return dict(row) if row else None


def save_address(addr: dict[str, Any]) -> dict:
    """存一条地址。已存在则只更新时间，不重复插入。"""
    data = normalize(addr)
    for key in ("country", "city", "line1"):
        if not data[key]:
            raise ValueError(f"账单地址缺少必填项：{key}")

    fp = _fingerprint(data)
    now = _now()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id FROM billing_addresses WHERE fingerprint=?", (fp,)).fetchone()
        if row:
            target_id = int(row["id"])
            conn.execute(
                "UPDATE billing_addresses SET updated_at=? WHERE id=?", (now, target_id))
        else:
            cur = conn.execute(
                "INSERT INTO billing_addresses"
                "(fingerprint, label, name, country, state, city, line1, line2, postal,"
                " use_count, last_used_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,NULL,?,?)",
                (fp, _label(data), data["name"], data["country"], data["state"],
                 data["city"], data["line1"], data["line2"], data["postal"], now, now))
            target_id = int(cur.lastrowid)
        conn.commit()
    # 连接关掉后再读，避免嵌套开连接
    return get_address(target_id) or {}


def mark_used(addr_id: int) -> None:
    """记一次使用，用于把常用地址排到前面。"""
    now = _now()
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE billing_addresses SET use_count=use_count+1, "
            "last_used_at=?, updated_at=? WHERE id=?", (now, now, int(addr_id)))
        conn.commit()


def delete_address(addr_id: int) -> bool:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM billing_address_cards WHERE address_id=?",
                     (int(addr_id),))
        cur = conn.execute("DELETE FROM billing_addresses WHERE id=?", (int(addr_id),))
        conn.commit()
    return cur.rowcount > 0


# ---- 卡 ↔ 地址关联 ----

def link_card_address(card_id: str, addr_id: int) -> None:
    """记一次"这张卡用了这个地址"。重复使用只累加次数并刷新时间。"""
    card_id = str(card_id or "").strip()
    if not card_id or not addr_id:
        return
    now = _now()
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO billing_address_cards"
            "(card_id, address_id, use_count, last_used_at, created_at)"
            " VALUES (?,?,1,?,?)"
            " ON CONFLICT(card_id, address_id) DO UPDATE SET"
            " use_count=use_count+1, last_used_at=excluded.last_used_at",
            (card_id, int(addr_id), now, now))
        conn.commit()


def addresses_for_card(card_id: str) -> list[int]:
    """这张卡用过的地址 id，最近用过的排在前面。"""
    card_id = str(card_id or "").strip()
    if not card_id:
        return []
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT c.address_id FROM billing_address_cards c"
            " JOIN billing_addresses a ON a.id = c.address_id"
            " WHERE c.card_id=? ORDER BY c.last_used_at DESC, c.address_id DESC",
            (card_id,)).fetchall()
    return [int(r["address_id"]) for r in rows]


def last_address_for_card(card_id: str) -> dict | None:
    """这张卡最近一次用过的地址。"""
    ids = addresses_for_card(card_id)
    return get_address(ids[0]) if ids else None


def card_links() -> dict[str, list[int]]:
    """全部卡 → 地址 id 列表（各自按最近使用排序），供前端一次拉取后本地分组。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT c.card_id, c.address_id FROM billing_address_cards c"
            " JOIN billing_addresses a ON a.id = c.address_id"
            " ORDER BY c.card_id, c.last_used_at DESC, c.address_id DESC").fetchall()
    out: dict[str, list[int]] = {}
    for row in rows:
        out.setdefault(str(row["card_id"]), []).append(int(row["address_id"]))
    return out
