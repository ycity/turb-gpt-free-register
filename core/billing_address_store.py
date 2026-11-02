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
        cur = conn.execute("DELETE FROM billing_addresses WHERE id=?", (int(addr_id),))
        conn.commit()
    return cur.rowcount > 0
