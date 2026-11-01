# -*- coding: utf-8 -*-
"""WebUI 代理池管理：列表、命名、启停、批量导入与连接测试结果。

代理列表仍保存在 .env 的 PROXY_POOL（行格式见 config.proxy），由 config_editor 统一读写；
连接测试结果属于运行状态，单独存入 turb.sqlite3，按代理 URL 的哈希关联，不落明文凭据。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from config.proxy import format_proxy_pool_line, normalize_proxy_url, split_proxy_pool_line
from core.proxy_utils import SUPPORTED_PROXY_SCHEMES, mask_proxy_url, parse_proxy_url
from webui import config_editor

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "turb.sqlite3"
NAME_MAX_LENGTH = 64
_POOL_KEY = "PROXY_POOL"
_WRITE_LOCK = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS proxy_pool_tests (
    proxy_key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    tested_at TEXT NOT NULL
)
"""


class ProxyPoolConflict(RuntimeError):
    """页面加载后，代理池已被其他页面或手动编辑 .env 修改。"""


def canonical_proxy_url(url: str) -> str:
    """统一成 scheme://user:pass@host:port（去掉路径、统一大小写和凭据编码），格式错误时抛 ValueError。"""
    spec = parse_proxy_url(url)
    auth = ""
    if spec.username or spec.password:
        auth = f"{quote(spec.username, safe='')}:{quote(spec.password, safe='')}@"
    host = f"[{spec.host}]" if ":" in spec.host else spec.host
    return f"{spec.scheme}://{auth}{host}:{spec.port}"


def _identity(url: str) -> str:
    """去重和关联测试结果用的标识：能解析就用规范形式，否则用原文。"""
    try:
        return canonical_proxy_url(url)
    except ValueError:
        return str(url)


def proxy_key(url: str) -> str:
    return hashlib.sha256(_identity(url).encode("utf-8")).hexdigest()[:24]


def pool_version(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def _current_lines() -> list[str]:
    return [str(line) for line in config_editor.get_field_value(_POOL_KEY) or []]


def _line_url(raw: str, default_scheme: str = "http") -> str:
    url, _, _ = split_proxy_pool_line(raw)
    return normalize_proxy_url(url, default_scheme=default_scheme) if url else ""


def _clean_name(value) -> str:
    return " ".join(str(value or "").split())


def _describe(index: int, raw: str, results: dict) -> dict:
    url, name, enabled = split_proxy_pool_line(raw)
    url = normalize_proxy_url(url)
    item = {
        "id": index, "raw": raw, "name": name, "enabled": enabled, "url": url,
        "valid": False, "error": "", "scheme": "", "host": "", "port": None,
        "username": "", "password": "", "key": "", "test": None,
    }
    try:
        spec = parse_proxy_url(url)
    except ValueError as exc:
        item["error"] = str(exc)
        return item
    key = proxy_key(url)
    item.update(
        valid=True, scheme=spec.scheme, host=spec.host, port=spec.port,
        username=spec.username, password=spec.password, key=key, test=results.get(key),
    )
    return item


def list_pool() -> dict:
    from config import proxy as proxy_cfg

    lines = _current_lines()
    results = _load_results()
    items = []
    for raw in lines:
        if _line_url(raw):
            items.append(_describe(len(items), raw, results))
    upstream = str(getattr(proxy_cfg, "PROXY_POOL_UPSTREAM_PROXY", "") or "").strip()
    return {
        "ok": True,
        "version": pool_version(lines),
        "items": items,
        "upstream": mask_proxy_url(upstream) if upstream else "",
    }


def save_pool(items, *, version: str | None = None) -> dict:
    """整表保存。

    未改动的行原样传 {raw}；只切换启停传 {raw, enabled}；新增/修改的行传 {name, url, enabled}。
    """
    if not isinstance(items, list):
        raise ValueError("items 必须是列表")
    with _WRITE_LOCK:
        current = _current_lines()
        if version and version != pool_version(current):
            raise ProxyPoolConflict("代理池已在其他页面或 .env 中被修改，请刷新后重试")
        lines = _build_lines(items)
        reloaded = _write_lines(lines)
        # 与写入放在同一临界区，避免用旧列表清掉并发导入的新代理的测试结果。
        _prune_results({proxy_key(url) for url in map(_line_url, lines) if url})
    return {**list_pool(), "reloaded": reloaded}


def _build_lines(items: list) -> list[str]:
    rows = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个代理格式错误")
        if "url" not in item:
            raw = str(item.get("raw") or "").replace("\r", " ").replace("\n", " ").strip()
            if raw and "enabled" in item:
                # 只翻转行首停用标记，地址和名称保持原文，旧配置里的重复行也能照常启停。
                body = raw[1:].strip() if raw.startswith("#") else raw
                raw = body if bool(item["enabled"]) else f"# {body}"
            if raw:
                rows.append((raw, _line_url(raw), False))
            continue
        try:
            url = canonical_proxy_url(normalize_proxy_url(str(item.get("url") or "").strip()))
        except ValueError as exc:
            raise ValueError(f"第 {index} 个代理：{exc}") from exc
        name = _clean_name(item.get("name"))
        if len(name) > NAME_MAX_LENGTH:
            raise ValueError(f"第 {index} 个代理的名称超过 {NAME_MAX_LENGTH} 个字符")
        rows.append((format_proxy_pool_line(url, name, bool(item.get("enabled", True))), url, True))

    identities = [_identity(url) if url else "" for _, url, _ in rows]
    for position, (_, url, edited) in enumerate(rows, 1):
        if not edited:
            continue
        for other, (other_line, _, _) in enumerate(rows, 1):
            if other != position and identities[other - 1] == identities[position - 1]:
                other_name = split_proxy_pool_line(other_line)[1]
                label = f"「{other_name}」" if other_name else ""
                raise ValueError(f"与第 {other} 个代理{label}地址相同：{mask_proxy_url(url)}")
    return [line for line, _, _ in rows]


def import_lines(text: str, *, default_scheme: str = "http", name_prefix: str = "",
                 version: str | None = None, dry_run: bool = False) -> dict:
    """批量导入：每行一个代理，完整 URL 可在行尾加 #名称；跳过重复和格式错误的行。"""
    scheme = str(default_scheme or "http").strip().lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(f"不支持的默认协议: {scheme}")
    prefix = _clean_name(name_prefix)
    if len(prefix) > NAME_MAX_LENGTH - 6:
        raise ValueError(f"名称前缀不能超过 {NAME_MAX_LENGTH - 6} 个字符")

    with _WRITE_LOCK:
        current = _current_lines()
        if version and not dry_run and version != pool_version(current):
            raise ProxyPoolConflict("代理池已在其他页面或 .env 中被修改，请刷新后重试")
        known = {_identity(url) for url in map(_line_url, current) if url}
        next_number = _next_prefix_number(prefix, [split_proxy_pool_line(raw)[1] for raw in current])
        added, invalid, duplicates = [], [], 0
        for line_no, raw in enumerate(str(text or "").splitlines(), 1):
            raw = raw.strip()
            url, name, enabled = split_proxy_pool_line(raw)
            if not url:
                continue
            try:
                url = canonical_proxy_url(normalize_proxy_url(url, default_scheme=scheme))
            except ValueError as exc:
                invalid.append({"line": line_no, "text": raw[:120], "error": str(exc)})
                continue
            if url in known:
                duplicates += 1
                continue
            known.add(url)
            name = _clean_name(name)[:NAME_MAX_LENGTH]
            if not name and prefix:
                name = f"{prefix} {next_number}"
                next_number += 1
            added.append({"url": url, "name": name, "enabled": enabled})

        result = {
            "ok": True,
            "dry_run": bool(dry_run),
            "added": len(added),
            "duplicates": duplicates,
            "invalid": invalid,
            "preview": [
                {"name": item["name"], "address": mask_proxy_url(item["url"]), "enabled": item["enabled"]}
                for item in added[:5]
            ],
        }
        if dry_run or not added:
            return {**result, "version": pool_version(current)}
        lines = current + [format_proxy_pool_line(item["url"], item["name"], item["enabled"]) for item in added]
        reloaded = _write_lines(lines)
    return {
        **result,
        **list_pool(),
        "reloaded": reloaded,
        "added_keys": [proxy_key(item["url"]) for item in added],
    }


def _next_prefix_number(prefix: str, names: list[str]) -> int:
    if not prefix:
        return 1
    pattern = re.compile(rf"^{re.escape(prefix)}\s*(\d+)$")
    numbers = [int(m.group(1)) for m in map(pattern.match, names) if m]
    return max(numbers, default=0) + 1


def check_proxy(url: str) -> dict:
    """测试单个代理并记录结果；url 可以是尚未保存的代理（编辑弹窗里先测再存）。"""
    from core.proxy_probe import probe_proxy

    url = normalize_proxy_url(str(url or "").strip())
    parse_proxy_url(url)
    result = probe_proxy(url)
    key = proxy_key(url)
    _save_result(key, result)
    return {"key": key, "result": result}


def _write_lines(lines: list[str]) -> bool:
    config_editor.update_config({_POOL_KEY: lines})
    try:
        import config as config_pkg
        config_pkg.reload_all()
        return True
    except Exception:
        logger.exception("代理池写入后热加载失败")
        return False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute(_SCHEMA)
    return conn


def tested_geo(url: str) -> dict:
    """返回代理池里这个代理最近一次测试拿到的出口地理信息。

    代理在加入池子时就测过出口 IP 和归属地，提链等场景可以直接复用，不必
    再做一次 geo 查询。没测过、测试失败或已不在池中时返回空 dict。
    """
    text = str(url or "").strip()
    if not text:
        return {}
    try:
        key = proxy_key(text)
    except Exception:
        return {}
    result = _load_results().get(key)
    if not isinstance(result, dict) or not result.get("ok"):
        return {}
    country = str(result.get("country") or "").strip()
    if not country:
        return {}
    return {
        "ip": str(result.get("ip") or ""),
        "country": country,
        "region": str(result.get("region") or ""),
        "city": str(result.get("city") or ""),
        "org": str(result.get("org") or ""),
    }


def _load_results() -> dict[str, dict]:
    try:
        with closing(_connect()) as conn:
            rows = conn.execute("SELECT proxy_key, result_json FROM proxy_pool_tests").fetchall()
    except sqlite3.Error:
        logger.warning("读取代理测试结果失败", exc_info=True)
        return {}
    results = {}
    for key, payload in rows:
        try:
            results[key] = json.loads(payload)
        except ValueError:
            continue
    return results


def _save_result(key: str, result: dict) -> None:
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO proxy_pool_tests (proxy_key, result_json, tested_at) VALUES (?, ?, ?) "
                "ON CONFLICT(proxy_key) DO UPDATE SET result_json = excluded.result_json, tested_at = excluded.tested_at",
                (key, json.dumps(result, ensure_ascii=False), str(result.get("tested_at") or "")),
            )
    except sqlite3.Error:
        logger.warning("保存代理测试结果失败", exc_info=True)


def _prune_results(keep: set[str]) -> None:
    """删掉已不在代理池里的测试结果，避免数据库无限增长。"""
    try:
        with closing(_connect()) as conn, conn:
            stale = [(key,) for (key,) in conn.execute("SELECT proxy_key FROM proxy_pool_tests") if key not in keep]
            conn.executemany("DELETE FROM proxy_pool_tests WHERE proxy_key = ?", stale)
    except sqlite3.Error:
        logger.warning("清理代理测试结果失败", exc_info=True)
