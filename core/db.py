# -*- coding: utf-8 -*-
"""
SQLite 持久化层（JSON/TXT 仅用于首次迁移）。

运行时数据全部存储在根目录 `turb.sqlite3`；旧 JSON/TXT/Codex 文件仅用于一次性迁移。
"""
import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

# 模块级 logger。
# 2026-11-18：line 1671 的 logger.exception 一直引用一个不存在的名字——
# 只因为那个 except 分支从没触发过才没暴露。分配订阅编号真出错时，
# 本该记日志的地方会抛 NameError，把原始异常盖掉。
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT
_LEGACY_DATA_DIR = _PROJECT_ROOT / "data"
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_PLAN_CHECK_STALE_SECONDS = 120
_PLAN_CHECK_QUEUE_STALE_SECONDS = 1800

_OUTLOOK_JSON = _PROJECT_ROOT / "用于注册的邮箱.json"
_OUTLOOK_TXT = _PROJECT_ROOT / "用于注册的邮箱.txt"
_GENERIC_API_EMAIL_JSON = _PROJECT_ROOT / "用于注册的API邮箱.json"
_GENERIC_API_EMAIL_TXT = _PROJECT_ROOT / "用于注册的API邮箱.txt"
_ACCOUNTS_JSON = _PROJECT_ROOT / "注册成功的邮箱.json"
_ACCOUNTS_TXT = _PROJECT_ROOT / "注册成功的邮箱.txt"
_TOKENS_TXT = _PROJECT_ROOT / "注册成功的token.txt"
_JOBS_JSON = _PROJECT_ROOT / "注册任务.json"
# 兼容旧测试/外部调用方；静态查看器已停用，不会再写入此路径。
_VIEWER_HTML = _PROJECT_ROOT / "accounts_viewer.html"
_CODEX_DIR = _PROJECT_ROOT / "codex_accounts"
_CODEX_AGENT_DIR = _PROJECT_ROOT / "codex_agent_accounts"
# 仅供一次性迁移旧导出状态，运行期间不再读取该文件。
_LEGACY_CODEX_EXPORT_STATE = _PROJECT_ROOT / "codex_导出状态.json"
# SQLite 是运行时唯一业务数据主存储；旧 JSON/TXT 仅用于一次性迁移。
_SQLITE_PATH = _PROJECT_ROOT / "turb.sqlite3"
_SQLITE_LOCK = threading.RLock()
_SQLITE_READY = False
_TABLES = {
    "accounts": "accounts",
    "outlook": "email_pool",
    "generic_api": "email_pool",
    "imap": "email_pool",
    "akmoz": "email_pool",
    "jobs": "registration_jobs",
    "domain": "email_pool",
    "codex": "codex_accounts",
}
_EMAIL_SOURCES = {"outlook": "outlook", "generic_api": "generic_api", "imap": "imap", "akmoz": "akmoz", "domain": "cloudflare_domain"}
_LEGACY_TABLES = {"outlook": "outlook_pool", "generic_api": "generic_api_pool", "domain": "domain_email_pool"}

_LEGACY_SQLITE = _LEGACY_DATA_DIR / "registrations.db"
_LEGACY_OUTLOOK_JSON = _LEGACY_DATA_DIR / "outlook_accounts.json"
_LEGACY_ACCOUNTS_JSON = _LEGACY_DATA_DIR / "registered_accounts.json"
_LEGACY_JOBS_JSON = _LEGACY_DATA_DIR / "registration_jobs.json"
_LOCK = threading.RLock()
_DEFAULT_SQLITE_PATH = _SQLITE_PATH
_DEFAULT_ACCOUNTS_JSON = _ACCOUNTS_JSON
_DEFAULT_OUTLOOK_JSON = _OUTLOOK_JSON
_DEFAULT_JOBS_JSON = _JOBS_JSON
_SQLITE_READY_PATH: Path | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_storage() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _sqlite_conn() -> sqlite3.Connection:
    """创建短生命周期连接；WAL 允许 WebUI 读与注册线程写并行。"""
    _ensure_storage()
    conn = sqlite3.connect(str(_active_sqlite_path()), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        # SQLite 内置 LOWER 只处理 ASCII；保持历史 Python str.lower 的邮箱匹配语义。
        conn.create_function("account_email_lower", 1, lambda value: str(value or "").lower(), deterministic=True)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


def _active_sqlite_path() -> Path:
    """测试替换旧 JSON 路径时使用同目录数据库，避免污染正式库。"""
    if (
        _ACCOUNTS_JSON != _DEFAULT_ACCOUNTS_JSON
        or _OUTLOOK_JSON != _DEFAULT_OUTLOOK_JSON
        or _JOBS_JSON != _DEFAULT_JOBS_JSON
    ):
        return _ACCOUNTS_JSON.parent / "turb.sqlite3"
    return _DEFAULT_SQLITE_PATH


def _read_legacy_sqlite_collection(collection: str) -> list[dict] | None:
    """读取旧 data/registrations.db 的数据，仅在一次性迁移阶段调用。"""
    if not _LEGACY_SQLITE.exists():
        return None
    try:
        with closing(sqlite3.connect(str(_LEGACY_SQLITE))) as legacy_conn:
            legacy_conn.row_factory = sqlite3.Row
            table = "registered_accounts" if collection == "accounts" else "outlook_pool" if collection == "outlook" else ""
            if not table or not _table_exists(legacy_conn, table):
                return None
            return [dict(row) for row in legacy_conn.execute(f"SELECT * FROM {table}").fetchall()]
    except Exception:
        return None


def _ensure_sqlite() -> None:
    """首次运行将现有 JSON 一次性导入 SQLite，之后 SQLite 为唯一读写源。"""
    global _SQLITE_READY, _SQLITE_READY_PATH
    active_path = _active_sqlite_path()
    if _SQLITE_READY and _SQLITE_READY_PATH == active_path:
        return
    with _SQLITE_LOCK:
        active_path = _active_sqlite_path()
        if _SQLITE_READY and _SQLITE_READY_PATH == active_path:
            return
        with closing(_sqlite_conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    PRIMARY KEY (id)
                );
                CREATE TABLE IF NOT EXISTS email_pool (
                    id INTEGER PRIMARY KEY,
                    email TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registration_jobs AS SELECT * FROM accounts WHERE 0;
                CREATE TABLE IF NOT EXISTS codex_accounts (
                    id INTEGER PRIMARY KEY,
                    filename TEXT NOT NULL UNIQUE, email TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS codex_agent_accounts (
                    account_id INTEGER PRIMARY KEY,
                    email TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_inspections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_account_inspection
                    ON account_inspections ((1)) WHERE status IN ('queued', 'running');
                CREATE TABLE IF NOT EXISTS account_inspection_snapshots (
                    inspection_id INTEGER PRIMARY KEY,
                    account_ids TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_inspection_failures (
                    inspection_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL,
                    failed_at TEXT NOT NULL,
                    PRIMARY KEY (inspection_id, account_id)
                );
                CREATE TABLE IF NOT EXISTS storage_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            for table in {"accounts", "email_pool", "registration_jobs"}:
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_status ON {table}(status, id DESC)")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_archived ON {table}(archived, id DESC)")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_email ON {table}(email COLLATE NOCASE)")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_created ON {table}(created_at DESC)")
            # registration_jobs 由 CREATE TABLE AS 建立，id 并不是主键，需显式索引。
            conn.execute("CREATE INDEX IF NOT EXISTS idx_registration_jobs_id ON registration_jobs(id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_registration_jobs_updated ON registration_jobs(updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_registration_jobs_root_status ON registration_jobs(CAST(json_extract(payload, '$.root_job_id') AS INTEGER), status, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email_lower ON accounts(account_email_lower(email), id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_email_pool_source_status ON email_pool(source, status, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_accounts_archived ON codex_accounts(archived, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_accounts_email ON codex_accounts(email COLLATE NOCASE)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_accounts_created ON codex_accounts(created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_agent_accounts_email ON codex_agent_accounts(email COLLATE NOCASE)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_agent_accounts_updated ON codex_agent_accounts(updated_at DESC)")
            migration_done = conn.execute(
                "SELECT 1 FROM storage_meta WHERE key='legacy_import_completed' LIMIT 1"
            ).fetchone()
            # 迁移标记写入 SQLite，而不是依赖“表是否为空”。这样用户删除全部数据后，
            # 重启也不会再次从旧 JSON 恢复已删除的数据。
            if not migration_done:
                sources = {
                    "accounts": (_ACCOUNTS_JSON, _LEGACY_ACCOUNTS_JSON),
                    "outlook": (_OUTLOOK_JSON, _LEGACY_OUTLOOK_JSON),
                    "generic_api": (_GENERIC_API_EMAIL_JSON,),
                    "jobs": (_JOBS_JSON, _LEGACY_JOBS_JSON),
                    "domain": (_DOMAIN_EMAIL_JSON,),
                }
                for collection, paths in sources.items():
                    table = _TABLES[collection]
                    exists = conn.execute(
                        f"SELECT 1 FROM {table}" + (" WHERE source=?" if table == "email_pool" else " LIMIT 1"),
                        ((_EMAIL_SOURCES[collection],) if table == "email_pool" else ()),
                    ).fetchone()
                    if exists:
                        continue
                    rows = None
                # 兼容上一版“records 单表 + collection”实现。
                    if _table_exists(conn, "records"):
                        legacy = conn.execute("SELECT payload FROM records WHERE collection=? ORDER BY id", (collection,)).fetchall()
                        if legacy:
                            rows = [json.loads(item["payload"]) for item in legacy]
                # 兼容上一版按邮箱来源拆分的三张表。
                    if collection in _EMAIL_SOURCES and rows is None:
                        old_table = _LEGACY_TABLES[collection]
                        if _table_exists(conn, old_table):
                            legacy = conn.execute(f"SELECT payload FROM {old_table} ORDER BY id").fetchall()
                            if legacy:
                                rows = [json.loads(item["payload"]) for item in legacy]
                    for path in paths:
                        if rows is None and path.exists():
                            candidate = _read_json(path, None)
                            if isinstance(candidate, list):
                                rows = candidate
                                break
                    if rows is None:
                        rows = _read_legacy_sqlite_collection(collection)
                    if not rows:
                        continue
                    next_email_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM email_pool").fetchone()[0]) + 1 if table == "email_pool" else 0
                    for pos, row in enumerate(rows, 1):
                        row = dict(row)
                        rid = next_email_id if table == "email_pool" else int(row.get("id") or pos)
                        if table == "email_pool":
                            next_email_id += 1
                        row["id"] = rid
                        conn.execute(
                            f"INSERT OR REPLACE INTO {table}(id,email,source,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?)" if table == "email_pool" else
                            f"INSERT OR REPLACE INTO {table}(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                            ((rid, str(row.get("email") or ""), _EMAIL_SOURCES[collection], str(row.get("status") or ""),
                              int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                              str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False)) if table == "email_pool" else
                             (rid, str(row.get("email") or ""), str(row.get("status") or ""),
                              int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                              str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False))),
                        )
            # 兼容早期 SQLite 版本的保存逻辑：当时写入 email_pool 时漏掉了
            # source 列，导致通用 API 邮箱在“全部邮箱池”中没有类型，按来源筛选
            # 也查不到。根据素材字段只修复可明确识别的历史行，避免误分类域名邮箱。
            conn.execute(
                "UPDATE email_pool SET source=? "
                "WHERE (source IS NULL OR trim(source)='') AND ("
                "json_extract(payload, '$.code_url') IS NOT NULL OR "
                "json_extract(payload, '$.url') IS NOT NULL OR "
                "json_extract(payload, '$.source') IN ('generic_api', 'generic-api') OR "
                "json_extract(payload, '$.email_source') IN ('generic_api', 'generic-api')"
                ")",
                (_EMAIL_SOURCES["generic_api"],),
            )
            conn.execute(
                "UPDATE email_pool SET source=? "
                "WHERE (source IS NULL OR trim(source)='') AND ("
                "json_extract(payload, '$.client_id') IS NOT NULL OR "
                "json_extract(payload, '$.clientId') IS NOT NULL OR "
                "json_extract(payload, '$.refresh_token') IS NOT NULL OR "
                "json_extract(payload, '$.refreshToken') IS NOT NULL OR "
                "json_extract(payload, '$.source') IN ('outlook', 'outlook_pool') OR "
                "json_extract(payload, '$.email_source') = 'outlook'"
                ")",
                (_EMAIL_SOURCES["outlook"],),
            )
            # 域名邮箱的历史 payload 没有 client_id/code_url 等特征，剩余的空来源
            # 记录只能归入域名邮箱池。否则它们会在“全部邮箱池”中显示为未知来源，
            # 前端又会按 Outlook 处理，导致列表里能看到但删除/改状态找不到。
            conn.execute(
                "UPDATE email_pool SET source=? "
                "WHERE (source IS NULL OR trim(source)='') AND COALESCE(("
                "json_extract(payload, '$.code_url') IS NOT NULL OR "
                "json_extract(payload, '$.url') IS NOT NULL OR "
                "json_extract(payload, '$.source') IN ('generic_api', 'generic-api', 'outlook', 'outlook_pool') OR "
                "json_extract(payload, '$.email_source') IN ('generic_api', 'generic-api', 'outlook') OR "
                "json_extract(payload, '$.client_id') IS NOT NULL OR "
                "json_extract(payload, '$.clientId') IS NOT NULL OR "
                "json_extract(payload, '$.refresh_token') IS NOT NULL OR "
                "json_extract(payload, '$.refreshToken') IS NOT NULL"
                "), 0)=0",
                (_EMAIL_SOURCES["domain"],),
            )
            # CPA Codex 凭证首次导入数据库；后续列表查询不再扫描 codex_accounts/ 文件。
            if not migration_done and not conn.execute("SELECT 1 FROM codex_accounts LIMIT 1").fetchone() and _CODEX_DIR.exists():
                state = _read_json(_LEGACY_CODEX_EXPORT_STATE, {})
                state = state if isinstance(state, dict) else {}
                for pos, path in enumerate(sorted(_CODEX_DIR.glob("codex-*.json")), 1):
                    try:
                        content = json.loads(path.read_text(encoding="utf-8"))
                        stat = path.stat()
                    except Exception:
                        continue
                    filename = path.name
                    meta = dict(content)
                    meta["_filename"] = filename
                    meta["_size"] = stat.st_size
                    meta["_mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
                    es = state.get(filename) or {}
                    meta["_exported_at"] = es.get("exported_at")
                    meta["_exported_count"] = es.get("exported_count", 0)
                    meta["_archived"] = bool(es.get("archived"))
                    conn.execute(
                        "INSERT OR IGNORE INTO codex_accounts(id,filename,email,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                        (pos, filename, str(content.get("email") or ""), int(meta["_archived"]), meta["_mtime"], meta["_mtime"], json.dumps(meta, ensure_ascii=False)),
                    )
            # Agent 凭证也只在首次迁移时读取；运行期间完整内容保存在 SQLite。
            if not migration_done and not conn.execute("SELECT 1 FROM codex_agent_accounts LIMIT 1").fetchone() and _CODEX_AGENT_DIR.exists():
                for path in sorted(_CODEX_AGENT_DIR.glob("codex-agent-*.json")):
                    try:
                        content = json.loads(path.read_text(encoding="utf-8"))
                        stat = path.stat()
                    except Exception:
                        continue
                    identity = content.get("agent_identity") if isinstance(content.get("agent_identity"), dict) else {}
                    email = str(content.get("email") or identity.get("email") or "").strip()
                    account = conn.execute("SELECT id, payload FROM accounts WHERE lower(email)=lower(?) LIMIT 1", (email,)).fetchone() if email else None
                    if not account:
                        continue
                    account_id = int(account["id"])
                    stamp = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
                    conn.execute(
                        "INSERT OR IGNORE INTO codex_agent_accounts(account_id,email,filename,created_at,updated_at,payload) VALUES(?,?,?,?,?,?)",
                        (account_id, email or str(json.loads(account["payload"]).get("email") or ""), path.name, stamp, stamp, json.dumps(content, ensure_ascii=False)),
                    )
                    account_payload = json.loads(account["payload"])
                    account_payload.setdefault("codex_agent_token", json.dumps(content, ensure_ascii=False))
                    account_payload.pop("codex_agent_auth_path", None)
                    conn.execute("UPDATE accounts SET payload=?, updated_at=? WHERE id=?", (json.dumps(account_payload, ensure_ascii=False), stamp, account_id))
            conn.commit()
            # 迁移完成后删除旧的通用表，避免运行时继续依赖它。
            for old_table in (*_LEGACY_TABLES.values(), "records"):
                if _table_exists(conn, old_table) and old_table not in _TABLES.values():
                    conn.execute(f"DROP TABLE {old_table}")
            if not migration_done:
                conn.execute("INSERT OR REPLACE INTO storage_meta(key, value) VALUES('legacy_import_completed', ?)", (_now(),))
            _reserve_account_id_floor(conn, int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM accounts").fetchone()[0]))
            _reserve_job_id_floor(conn, int(conn.execute("SELECT COALESCE(MAX(id),0) FROM registration_jobs").fetchone()[0]))
            _migrate_inspection_snapshots(conn)
        _SQLITE_READY = True
        _SQLITE_READY_PATH = active_path


def _load_collection(collection: str) -> list[dict]:
    _ensure_sqlite()
    table = _TABLES[collection]
    with closing(_sqlite_conn()) as conn:
        with conn:
            sql = f"SELECT payload FROM {table}"
            params: tuple[str, ...] = ()
            if table == "email_pool":
                sql += " WHERE source=?"; params = (_EMAIL_SOURCES[collection],)
            sql += " ORDER BY id"
            return [json.loads(row["payload"]) for row in conn.execute(sql, params)]


def _save_collection(collection: str, rows: list[dict]) -> None:
    _ensure_sqlite()
    table = _TABLES[collection]
    source = _EMAIL_SOURCES[collection] if table == "email_pool" else None
    with closing(_sqlite_conn()) as conn:
        with conn:
            if table == "accounts":
                # 全量重写账号表时也保留历史最大 ID；删除最后一个/全部账号不能重用 ID。
                floor = max([int(r.get("id") or 0) for r in rows] + [
                    int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM accounts").fetchone()[0])])
                _reserve_account_id_floor(conn, floor)
            if table == "registration_jobs":
                floor = max([int(r.get("id") or 0) for r in rows] + [
                    int(conn.execute("SELECT COALESCE(MAX(id),0) FROM registration_jobs").fetchone()[0])])
                _reserve_job_id_floor(conn, floor)
            if table == "email_pool":
                conn.execute("DELETE FROM email_pool WHERE source=?", (source,))
            else:
                conn.execute(f"DELETE FROM {table}")
            for pos, raw in enumerate(rows, 1):
                row = dict(raw)
                rid = int(row.get("id") or pos)
                row["id"] = rid
                if table == "email_pool" and conn.execute("SELECT 1 FROM email_pool WHERE id=?", (rid,)).fetchone():
                    rid = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM email_pool").fetchone()[0])
                    row["id"] = rid
                if table == "email_pool":
                    conn.execute(
                        "INSERT INTO email_pool(id,email,source,status,archived,created_at,updated_at,payload) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (rid, str(row.get("email") or ""), source, str(row.get("status") or ""),
                         int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                         str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False)),
                    )
                else:
                    conn.execute(
                        f"INSERT INTO {table}(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                        (rid, str(row.get("email") or ""), str(row.get("status") or ""),
                         int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                         str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False)),
                    )


def _query_collection(collection: str, *, status: str | None = None, archived: str | bool | None = None,
                       q: str | None = None, date_from: str | None = None, date_to: str | None = None,
                       limit: int | None = None, offset: int = 0) -> list[dict]:
    """利用索引分页读取，避免 WebUI 为一个页面加载整个 JSON 文件。"""
    _ensure_sqlite()
    table = _TABLES[collection]
    where = ["1=1"]
    params: list[Any] = []
    if table == "email_pool":
        where.append("source=?"); params.append(_EMAIL_SOURCES[collection])
    if status:
        where.append("status=?"); params.append(status)
    if archived not in (None, "all", "include"):
        where.append("archived=?"); params.append(int(archived in (True, "1", "true", "yes", "only")))
    if q and str(q).strip():
        where.append("lower(payload) LIKE ?"); params.append("%" + str(q).strip().lower() + "%")
    if date_from:
        where.append("created_at >= ?"); params.append(str(date_from) + ("T00:00:00" if len(str(date_from)) == 10 else ""))
    if date_to:
        value = str(date_to)
        where.append("created_at <= ?"); params.append(value + ("T23:59:59.999999" if len(value) == 10 else ""))
    sql = f"SELECT payload FROM {table} WHERE " + " AND ".join(where) + " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"; params.extend([max(0, int(limit)), max(0, int(offset))])
    with closing(_sqlite_conn()) as conn:
        return [json.loads(row["payload"]) for row in conn.execute(sql, params)]


def _query_collection_page(collection: str, *, status: str | None = None,
                           archived: str | bool | None = None, q: str | None = None,
                           date_from: str | None = None, date_to: str | None = None,
                           extra_where: list[str] | None = None,
                           extra_params: list[Any] | None = None,
                           order_by: str | None = None,
                           limit: int = 50, offset: int = 0) -> tuple[list[dict], int, str]:
    """执行真正的 SQL COUNT/LIMIT/OFFSET 分页，并返回最新更新时间。

    order_by 只接受调用方写死的字符串，不能来自用户输入（会拼进 SQL）。
    不传就按 id DESC，保持其它集合的原有行为。
    """
    _ensure_sqlite()
    table = _TABLES[collection]
    where = ["1=1"]
    params: list[Any] = []
    if table == "email_pool":
        where.append("source=?"); params.append(_EMAIL_SOURCES[collection])
    if status:
        where.append("status=?"); params.append(status)
    if archived not in (None, "all", "include"):
        where.append("archived=?"); params.append(int(archived in (True, "1", "true", "yes", "only")))
    if q and str(q).strip():
        where.append("lower(payload) LIKE ?"); params.append("%" + str(q).strip().lower() + "%")
    if date_from:
        value = str(date_from)
        where.append("created_at >= ?"); params.append(value + ("T00:00:00" if len(value) == 10 else ""))
    if date_to:
        value = str(date_to)
        where.append("created_at <= ?"); params.append(value + ("T23:59:59.999999" if len(value) == 10 else ""))
    if extra_where:
        where.extend(extra_where)
        params.extend(extra_params or [])
    clause = " AND ".join(where)
    order_sql = order_by or "id DESC"
    with closing(_sqlite_conn()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}", params).fetchone()[0])
        latest = str(conn.execute(f"SELECT COALESCE(MAX(updated_at), '') FROM {table} WHERE {clause}", params).fetchone()[0] or "")
        rows = [json.loads(row["payload"]) for row in conn.execute(
            f"SELECT payload FROM {table} WHERE {clause} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*params, max(1, int(limit)), max(0, int(offset))],
        )]
    return rows, total, latest


# 账号列表排序：没开通订阅的排前面（按 id 降序，新注册的先看到），
# 已开通 team 的排后面（按下次账单时间降序，快到期的先看到）。
# 写死在这里而不是接收参数：order_by 会拼进 SQL，不能来自用户输入。
_ACCOUNTS_ORDER_BY = (
    "CASE WHEN lower(COALESCE(json_extract(payload,'$.plan_type'),'')) = 'team' "
    "THEN 1 ELSE 0 END ASC, "
    "COALESCE(json_extract(payload,'$.plan_renews_at'), "
    "json_extract(payload,'$.renews_at'), '') DESC, "
    "id DESC"
)


# 算作"已订阅"的套餐关键字。
#
# 为什么不只看 plus：这套号实际开的是 Team，库里 current_plan_type 就是
# 'team'。原先筛选写死 "plus" in plan，于是 36 个已订阅的 Team 号一个都
# 筛不出来（2026-11-17 实测：plus 命中 0 条）。
#
# 与 webui/app.py 里决定是否展示 expires_at 的那组关键字保持一致。
# free 不在此列，且带 free 的取值一律排除（"free(可Plus试用)"只是资格）。
_SUBSCRIBED_PLAN_KEYWORDS = ("plus", "pro", "team", "go", "business")


def _account_filter_sql(
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    totp_filter: str | None = None,
    seat_filter: str | None = None,
) -> tuple[list[str], list[Any]]:
    """把账号列表的套餐、Codex、2FA、席位过滤条件下推到 SQLite。

    套餐、Codex、2FA 状态仍保存在账号 payload 中，因此这里使用 SQLite JSON1
    直接过滤，而不是先把整张 accounts 表反序列化到 Python 再切页。
    """
    where: list[str] = []
    params: list[Any] = []
    plan = str(plan_filter or "").strip().lower()
    codex = str(codex_filter or "").strip().lower()
    totp = str(totp_filter or "").strip().lower()
    seat = str(seat_filter or "").strip().lower()

    plan_expr = (
        "lower(COALESCE(NULLIF(CAST(json_extract(payload, '$.current_plan_type') AS TEXT), ''), "
        "CAST(json_extract(payload, '$.plan_type') AS TEXT), ''))"
    )
    if plan and plan not in {"all", "any"}:
        if plan in {"subscribed", "plus"}:
            # 已订阅：套餐是 plus/pro/team/go/business 任一，且不含 free。
            # "plus" 仍然接受，是为了兼容老前端存下的筛选值。
            ors = " OR ".join([f"{plan_expr} LIKE ?"] * len(_SUBSCRIBED_PLAN_KEYWORDS))
            where.append(f"({ors})")
            params.extend([f"%{k}%" for k in _SUBSCRIBED_PLAN_KEYWORDS])
            where.append(f"{plan_expr} NOT LIKE ?")
            params.append("%free%")
        elif plan in {"unsubscribed", "not_subscribed", "no_plan"}:
            # 未订阅：套餐查过了但不是任何付费套餐（通常是 free）。
            #
            # 含 free 的一律算未订阅，且这条优先于关键字匹配——
            # "free(可Plus试用)" 里带着 plus 字样，只按关键字判会被当成
            # 已订阅而两边都漏掉，和上面 subscribed 的 free 排除规则对称。
            #
            # 必须排除"还没查过套餐"的账号：那种 plan 字段是空的，归入
            # 未订阅会把一堆状态未知的号混进来，看着像能订阅实际不知道。
            ors = " AND ".join([f"{plan_expr} NOT LIKE ?"] * len(_SUBSCRIBED_PLAN_KEYWORDS))
            where.append(f"({plan_expr} LIKE ? OR ({ors}))")
            params.append("%free%")
            params.extend([f"%{k}%" for k in _SUBSCRIBED_PLAN_KEYWORDS])
            where.append(f"{plan_expr} != ''")
        elif plan in {"plus_trial", "plus_trial_eligible", "trial", "trial_eligible"}:
            # 只有当前套餐为 free 且套餐查询明确返回可试用资格时才命中。
            trial_expr = "lower(COALESCE(CAST(json_extract(payload, '$.plus_trial_eligible') AS TEXT), ''))"
            where.append(f"{plan_expr} = ?")
            where.append(f"{trial_expr} IN (?, ?, ?, ?)")
            params.extend(["free", "1", "true", "yes", "on"])
        elif plan in {"promo", "promotion", "plan_promo", "eligible_promo"} or plan.startswith("promo:"):
            # 当前套餐必须是 free，并且任意套餐存在至少一个可用优惠活动；
            # 不限定 Plus。eligible_promo_campaigns 是以套餐名为 key 的对象。
            promo_expr = "json_extract(payload, '$.eligible_promo_campaigns')"
            where.append(f"{plan_expr} = ?")
            where.append("json_type(payload, '$.eligible_promo_campaigns') = 'object'")
            where.append(f"EXISTS (SELECT 1 FROM json_each({promo_expr}))")
            params.append("free")
            promo_parts = plan.split(":") if plan.startswith("promo:") else []
            promo_type = promo_parts[1].strip()[:64] if len(promo_parts) > 1 else ""
            promo_discount = promo_parts[2].strip()[:16] if len(promo_parts) > 2 else ""
            conditions: list[str] = []
            condition_params: list[Any] = []
            if promo_type and promo_type not in {"*", "all", "any"}:
                canonical = promo_type.lower().replace("-", "").replace("_", "").replace(" ", "")
                if canonical.startswith("chatgpt"):
                    canonical = canonical[7:]
                if canonical.endswith("plan"):
                    canonical = canonical[:-4]
                normalized_key = "lower(replace(replace(replace(j.key, '-', ''), '_', ''), ' ', ''))"
                normalized_name = (
                    "lower(replace(replace(replace(COALESCE(CAST(json_extract(j.value, '$.metadata.plan_name') AS TEXT), ''), "
                    "'-', ''), '_', ''), ' ', ''))"
                )
                conditions.append(
                    f"({normalized_key} = ? OR {normalized_name} IN (?, ?, ?))"
                )
                condition_params.extend([canonical, canonical, f"{canonical}plan", f"chatgpt{canonical}plan"])
            if promo_discount == "other":
                # 仅收录明确有效的其他优惠比例，未知/无优惠不归入“其他”。
                percentage = "json_extract(j.value, '$.metadata.discount.percentage')"
                conditions.extend([
                    "json_type(j.value, '$.metadata.discount.percentage') IN ('integer', 'real')",
                    f"{percentage} > 0 AND {percentage} <= 100",
                    f"{percentage} NOT IN (50, 100)",
                ])
            elif promo_discount:
                try:
                    discount_value = float(promo_discount.rstrip("%"))
                except ValueError:
                    discount_value = None
                if discount_value is not None:
                    conditions.append("CAST(json_extract(j.value, '$.metadata.discount.percentage') AS REAL) = ?")
                    condition_params.append(discount_value)
            if conditions:
                where.append(
                    f"EXISTS (SELECT 1 FROM json_each({promo_expr}) AS j WHERE "
                    + " AND ".join(conditions) + ")"
                )
                params.extend(condition_params)
        elif plan in {"free_no_trial", "free_without_trial", "free_not_trial"}:
            # 只匹配已成功查询且没有任何套餐优惠的 free 账号；字段缺失代表
            # 尚未得到完整优惠结果，不应归入“不可试用套餐”。
            promo_expr = "json_extract(payload, '$.eligible_promo_campaigns')"
            where.append(f"{plan_expr} = ?")
            where.append("json_type(payload, '$.eligible_promo_campaigns') = 'object'")
            where.append(f"NOT EXISTS (SELECT 1 FROM json_each({promo_expr}))")
            params.append("free")
        elif plan == "free":
            where.append(f"{plan_expr} = ?")
            params.append("free")
        else:
            where.append(f"{plan_expr} = ?")
            params.append(plan)

    status_expr = "lower(COALESCE(CAST(json_extract(payload, '$.codex_status') AS TEXT), ''))"
    live_status_expr = "lower(COALESCE(CAST(json_extract(payload, '$.live_check_status') AS TEXT), ''))"
    if codex and codex not in {"all", "*"}:
        if codex == "deactivated":
            where.append(f"{live_status_expr} = ?")
        else:
            where.append(f"{status_expr} = ?")
        params.append(codex)

    totp_secret_expr = "lower(COALESCE(CAST(json_extract(payload, '$.totp_secret') AS TEXT), ''))"
    totp_setup_expr = "lower(COALESCE(CAST(json_extract(payload, '$.totp_setup_status') AS TEXT), ''))"
    if totp and totp not in {"all", "*"}:
        if totp in {"enabled", "on", "active"}:
            where.append(f"length(trim({totp_secret_expr})) > 0")
        elif totp in {"disabled", "off", "not_enabled", "unset"}:
            where.append(f"length(trim({totp_secret_expr})) = 0")
        elif totp in {"pending", "setup", "setting", "queued", "running"}:
            where.append(f"{totp_setup_expr} IN (?, ?)")
            params.extend(["queued", "running"])
        elif totp == "failed":
            where.append(f"{totp_setup_expr} = ?")
            params.append("failed")
        elif totp == "stopped":
            where.append(f"{totp_setup_expr} = ?")
            params.append("stopped")
        else:
            where.append(f"{totp_setup_expr} = ?")
            params.append(totp)

    # 席位筛选。single_seat_supported 是三态：
    #   True  -> 确定支持 1 席
    #   False -> 确定要 2 席（订阅要多花一倍钱）
    #   缺失  -> 还没探测过，不知道
    #
    # "过滤2席位"要滤掉的只有确定为 False 的那批；没探过的必须留下，
    # 它们里面还有一半左右是单席位（实测命中率约 55%）。
    seat_expr = "json_extract(payload, '$.single_seat_supported')"
    if seat and seat not in {"all", "*"}:
        if seat in {"not_multi", "hide_multi", "exclude_multi"}:
            # 排除确定 2 席的：JSON 里 false 存成 0
            where.append(f"({seat_expr} IS NULL OR {seat_expr} != 0)")
        elif seat in {"single", "single_only", "one"}:
            where.append(f"{seat_expr} = 1")
        elif seat in {"multi", "multi_only", "two"}:
            where.append(f"{seat_expr} = 0")
        elif seat in {"unknown", "unprobed", "not_checked"}:
            where.append(f"{seat_expr} IS NULL")
        elif seat in {"free_trial", "free", "team_free_trial"}:
            where.append("json_extract(payload, '$.team_free_trial') = 1")
        elif seat in {"discount", "has_discount"}:
            where.append(
                "(json_extract(payload, '$.team_free_trial') = 1 "
                "OR COALESCE(json_extract(payload, '$.team_checkout_discount_pct'), 0) > 0 "
                "OR COALESCE(json_extract(payload, '$.team_checkout_discount_minor'), 0) > 0)"
            )
    return where, params


def _pool_summary_sql(collection: str) -> dict:
    _ensure_sqlite()
    table = _TABLES[collection]
    with closing(_sqlite_conn()) as conn:
        where = " WHERE source=?" if table == "email_pool" else ""
        params = (_EMAIL_SOURCES[collection],) if table == "email_pool" else ()
        counts = {str(r["status"] or "available"): int(r["n"]) for r in conn.execute(
            f"SELECT status, COUNT(*) AS n FROM {table}{where} GROUP BY status", params
        )}
    out = {"available": counts.get("available", 0), "used": counts.get("used", 0), "failed": counts.get("failed", 0)}
    out.update({k: v for k, v in counts.items() if k not in out})
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def _read_json(path: Path, default: Any) -> Any:
    _ensure_storage()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def get_meta(key: str) -> str | None:
    """读取 storage_meta 键值；缺失返回 None。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        row = conn.execute("SELECT value FROM storage_meta WHERE key=?", (str(key),)).fetchone()
    return str(row[0]) if row else None


def set_meta(key: str, value: str) -> None:
    """写入 storage_meta 键值，供需要跨重启保留的小状态使用。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO storage_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), str(value)),
        )


def _reserve_account_id_floor(conn, value: int) -> None:
    conn.execute("""INSERT INTO storage_meta(key,value) VALUES('account_id_high_watermark',?)
                    ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER))""",
                 (str(value),))


def _next_account_id() -> int:
    """事务内预留单调递增 ID，防止删除后旧任务操作到新账号。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM storage_meta WHERE key='account_id_high_watermark'").fetchone()
        existing = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM accounts").fetchone()[0])
        value = max(int(row[0]) if row else 0, existing) + 1
        _reserve_account_id_floor(conn, value)
        return value


def _next_id(items: list[dict]) -> int:
    ids = [int(item.get("id") or 0) for item in items]
    return (max(ids) if ids else 0) + 1


def _outlook_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("password") or "",
        row.get("client_id") or "",
        row.get("refresh_token") or "",
    ])


def _generic_api_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        _normalize_generic_api_code_url(row.get("code_url")),
    ])


def _normalize_generic_api_code_url(value: object) -> str:
    """修复导入文本中误粘贴到 URL 前面的短横线。"""
    url = str(value or "").strip()
    if url.startswith("-"):
        candidate = url.lstrip("-")
        if candidate.lower().startswith(("http://", "https://")):
            return candidate
    return url


def _imap_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("imap_password") or row.get("password") or "",
    ])


def _extract_registration_password(row: dict) -> str:
    extra_raw = row.get("extra_json")
    if isinstance(extra_raw, str) and extra_raw.strip():
        try:
            extra = json.loads(extra_raw)
        except Exception:
            extra = {}
    elif isinstance(extra_raw, dict):
        extra = extra_raw
    else:
        extra = {}
    return str(extra.get("registration_password") or row.get("registration_password") or "").strip()


def _looks_like_email_material_segment(segment: str) -> bool:
    seg = str(segment or "").strip()
    if not seg:
        return False
    if seg.startswith("M.") or seg.startswith("m."):
        return True
    if len(seg) >= 32 and "-" in seg and seg.count("-") >= 4:
        return True
    if any(ch in seg for ch in ("@", ":", "/", "\\")):
        return True
    return False


def _ensure_password_in_material_line(base: str, password: str) -> str:
    base = str(base or "").strip()
    password = str(password or "").strip()
    if not password:
        return base
    parts = [p for p in base.split("----") if p != ""] if base else []
    if not parts:
        return password
    if len(parts) == 1:
        if parts[0] == password:
            return base
        return "----".join([parts[0], password])
    if parts[1] == password:
        return base
    if _looks_like_email_material_segment(parts[1]):
        parts.insert(1, password)
        return "----".join(parts)
    return base


def _account_line(row: dict) -> str:
    base = row.get("original_email_line") or row.get("email") or ""
    email_password = str(row.get("password") or "").strip()
    base = _ensure_password_in_material_line(base, email_password)
    token = row.get("access_token") or ""
    gpt_password = _extract_registration_password(row) or "未设置"
    totp = row.get("totp_secret") or ""
    parts = [base, token, gpt_password]
    if totp:
        parts.append(totp)
    return "----".join(parts)


# “完整导出”里 2FA 段固定的站点占位（需求指定的固定文案）。
_TWOFA_EXPORT_URL = "https://2fa.run/"


def _account_full_export_line(row: dict) -> str:
    """生成“完整导出”单行，格式严格按需求：

        邮箱---邮箱接码API---密码---https://2fa.run/----2FA:密钥

    - 邮箱接码API：接码所用的“完整接码链接格式”。generic_api 账号直接输出邮箱池里
      存的 code_url（取码地址，如 http://127.0.0.1:5055/code?email=xxx@domain）；
      其它来源（gptmail/outlook/remail…）保留来源标识。
    - 密码：ChatGPT 账号自身登录密码（registration_password）。
    - 2FA：固定前缀 “2FA:” 拼接 TOTP 密钥。
    分隔符：前四段之间为 “---”，2FA 段之前为 “----”（与需求保持一致）。
    """
    email = str(row.get("email") or "").strip()
    email_api = _resolve_email_api_link(email, str(row.get("email_source") or "").strip())
    # 仅填 ChatGPT 注册密码；若该账号没有，则留空。
    password = _extract_registration_password(row)
    totp = str(row.get("totp_secret") or "").strip()
    line = "---".join([email, email_api, password, _TWOFA_EXPORT_URL])
    line = line + "----" + ("2FA:" + totp)
    return line


def _resolve_email_api_link(email: str, email_source: str) -> str:
    """把“邮箱接码API”字段解析为完整接码链接格式。

    - generic_api：优先取邮箱池里的 code_url（取码地址）作为完整链接；
      池里没有该邮箱时，按 OmniMail 取码接口约定拼出完整链接
      （{OMNIMAIL_BASE}/messages?mailbox=<邮箱>），仍然取不到才回退为原文。
    - 其它来源：原样返回来源标识。
    """
    if email_source == "generic_api" and email:
        try:
            pool_row = get_generic_api_email_by_email(email)
        except Exception:
            pool_row = None
        if pool_row:
            link = str(pool_row.get("code_url") or "").strip()
            if link:
                return link
        link = _build_generic_api_code_url(email)
        if link:
            return link
    return email_source


def _build_generic_api_code_url(email: str) -> str:
    """按 OmniMail 取码接口约定拼出完整取码链接；取不到基础地址时返回空串。"""
    try:
        # 延迟导入，避免 core.db 与 config 包产生循环依赖。
        from config.email import OMNIMAIL_BASE
        base = str(OMNIMAIL_BASE or "").strip().rstrip("/")
    except Exception:
        base = ""
    if not base or not email:
        return ""
    return f"{base}/messages?mailbox={email}"


def _registered_email_line(row: dict) -> str:
    """生成注册成功邮箱 TXT 的行内容；token 由注册成功的token.txt 单独保存。"""
    return row.get("original_email_line") or row.get("email") or ""


def _load_outlook() -> list[dict]:
    return _load_collection("outlook")


def _save_outlook(rows: list[dict]) -> None:
    _save_collection("outlook", rows)


def _load_generic_api_emails() -> list[dict]:
    rows = _load_collection("generic_api")
    changed = False
    for row in rows:
        original = row.get("code_url")
        normalized = _normalize_generic_api_code_url(original)
        if normalized != original:
            row["code_url"] = normalized
            changed = True
    if changed:
        _save_generic_api_emails(rows)
    return rows


def _save_generic_api_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _generic_api_email_line(row)
    _save_collection("generic_api", rows)


def _load_imap_emails() -> list[dict]:
    return _load_collection("imap")


# ============================================================
# akmoz（闪取）邮箱池
# ============================================================

def _akmoz_email_line(row: dict) -> str:
    """复制出去的素材行，和导入格式保持一致，能原样粘回来。"""
    return "----".join([
        str(row.get("email") or ""),
        str(row.get("password") or ""),
        str(row.get("code_url") or ""),
    ])


def _load_akmoz_emails() -> list[dict]:
    return _load_collection("akmoz")


def _save_akmoz_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _akmoz_email_line(row)
    _save_collection("akmoz", rows)


def import_akmoz_emails(records: list[dict]) -> tuple[int, int]:
    """批量导入 akmoz 邮箱。records 元素：{email, password, code_url}。

    返回 (新增数, 跳过数)。同邮箱已存在就跳过，不覆盖——
    已经用过的邮箱重新导入会把 used 状态洗掉，那样会重复注册。
    """
    with _LOCK:
        rows = _load_akmoz_emails()
        inserted = skipped = 0
        for raw in records or []:
            email = str(raw.get("email") or "").strip()
            if not email or "@" not in email:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": str(raw.get("password") or "").strip(),
                "code_url": str(raw.get("code_url") or "").strip(),
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _akmoz_email_line(row)
            rows.append(row)
            inserted += 1
        _save_akmoz_emails(rows)
        return inserted, skipped


def claim_next_akmoz_email() -> dict | None:
    """原子领取一个可用 akmoz 邮箱并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_akmoz_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows
                    if str(r.get("status") or "") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        _save_akmoz_emails(rows)
        return dict(row)


def release_akmoz_email(email: str, status: str = "available",
                        note: str | None = None) -> None:
    with _LOCK:
        rows = _load_akmoz_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = str(status or "available")
        if status == "available":
            row["used_at"] = None
        if note is not None:
            row["note"] = str(note)[:300]
        _save_akmoz_emails(rows)


def release_unconsumed_akmoz_email(email: str, note: str | None = None) -> bool:
    """注册没用上就把邮箱退回池子，别白白消耗。"""
    with _LOCK:
        rows = _load_akmoz_emails()
        row = _find_by_email(rows, email)
        if row is None or str(row.get("status") or "") != "used":
            return False
        if get_account_by_email(email):
            return False          # 已经注册出账号了，不能退
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = str(note)[:300]
        _save_akmoz_emails(rows)
        return True


def delete_akmoz_email(email: str) -> bool:
    with _LOCK:
        rows = _load_akmoz_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return False
        rows.remove(row)
        _save_akmoz_emails(rows)
        return True


def list_akmoz_email_pool(status: str | None = None,
                          limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="akmoz", status=status, limit=limit, offset=0)["items"]


def akmoz_email_pool_summary() -> dict:
    with _LOCK:
        return _pool_summary_sql("akmoz")


def get_akmoz_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_akmoz_emails(), email)
        return dict(row) if row else None


def _save_imap_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _imap_email_line(row)
    _save_collection("imap", rows)


def _load_accounts() -> list[dict]:
    return _load_collection("accounts")


def _select_account(conn, acc_id: int | None = None, email: str | None = None) -> dict | None:
    """按主键读取一行；兼容历史 ID / 邮箱 OR 匹配时的首个账号语义。"""
    target_email = (email or "").lower()
    if acc_id is not None and not target_email:
        record = conn.execute("SELECT payload FROM accounts WHERE id=?", (int(acc_id),)).fetchone()
    elif target_email:
        record = conn.execute(
            "SELECT payload FROM accounts WHERE id=? OR account_email_lower(email)=? ORDER BY id LIMIT 1",
            (int(acc_id) if acc_id is not None else None, target_email),
        ).fetchone()
    else:
        return None
    return json.loads(record["payload"]) if record else None


@contextmanager
def _account_for_update(acc_id: int | None = None, email: str | None = None):
    """调用方持有 _LOCK；只读改写目标行，不全量重写账号集合。

    SQLite 事务覆盖领取校验与写回；异常自动回滚，不插入已删除账号。
    """
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _select_account(conn, acc_id, email)
        before = json.dumps(row, ensure_ascii=False)
        yield row
        if row is None or json.dumps(row, ensure_ascii=False) == before:
            return
        row["copy_line"] = _account_line(row)
        conn.execute(
            "UPDATE accounts SET email=?,status=?,archived=?,created_at=?,updated_at=?,payload=? WHERE id=?",
            (str(row.get("email") or ""), str(row.get("status") or ""),
             int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
             str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False), int(row["id"])),
        )


def _save_accounts(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _account_line(row)
    _save_collection("accounts", rows)


def _load_jobs() -> list[dict]:
    return _load_collection("jobs")


def _save_jobs(rows: list[dict]) -> None:
    _save_collection("jobs", rows)


def _find_by_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def _decorate_account(row: dict) -> dict:
    out = dict(row)
    out["note"] = out.get("note") or ""
    out["note_updated_at"] = out.get("note_updated_at") or ""
    plan_status = out.get("plan_check_status")
    if plan_status in {"queued", "running"}:
        try:
            stamp_key = "plan_check_queued_at" if plan_status == "queued" else "plan_check_started_at"
            stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if plan_status == "queued" else _PLAN_CHECK_STALE_SECONDS
            started_at = datetime.fromisoformat(str(out.get(stamp_key) or ""))
            if (datetime.now() - started_at).total_seconds() >= stale_after:
                out["plan_check_status"] = "failed"
                out["plan_check_error"] = "上次套餐查询状态已超时，可重新查询"
                out["plan_check_stale"] = True
        except (TypeError, ValueError):
            out["plan_check_status"] = "failed"
            out["plan_check_error"] = "上次套餐查询状态异常，可重新查询"
            out["plan_check_stale"] = True
    out["copy_line"] = _account_line(out)
    return out


def _account_matches_plan_filter(row: dict, plan_filter: str | None = None) -> bool:
    """账号套餐过滤：支持已订阅、未订阅、任意套餐优惠及 free 资格过滤。"""
    f = str(plan_filter or "").strip().lower()
    if not f or f in {"all", "any"}:
        return True
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    if f in {"subscribed", "plus"}:
        # 已订阅：plus/pro/team/go/business 任一；"free(可Plus试用)"只是
        # 资格，不算已订阅。"plus" 保留是为了兼容老前端存下的筛选值。
        return (any(k in plan for k in _SUBSCRIBED_PLAN_KEYWORDS)
                and "free" not in plan)
    if f in {"unsubscribed", "not_subscribed", "no_plan"}:
        # 未订阅：查过套餐（plan 非空）且不是任何付费套餐。
        # 含 free 的一律算未订阅，且优先于关键字匹配——"free(可Plus试用)"
        # 里带着 plus 字样，只按关键字判会两边都漏掉。
        # plan 为空代表还没查过，状态未知，不归入未订阅。
        if not plan:
            return False
        if "free" in plan:
            return True
        return not any(k in plan for k in _SUBSCRIBED_PLAN_KEYWORDS)
    if f in {"plus_trial", "plus_trial_eligible", "trial", "trial_eligible"}:
        trial = row.get("plus_trial_eligible")
        if isinstance(trial, str):
            trial = trial.strip().lower() in {"1", "true", "yes", "on"}
        return plan == "free" and bool(trial)
    if f in {"promo", "promotion", "plan_promo", "eligible_promo"} or f.startswith("promo:"):
        campaigns = row.get("eligible_promo_campaigns")
        if plan != "free" or not isinstance(campaigns, dict) or not campaigns:
            return False
        promo_parts = f.split(":") if f.startswith("promo:") else []
        wanted = promo_parts[1].strip() if len(promo_parts) > 1 else ""
        discount_text = promo_parts[2].strip() if len(promo_parts) > 2 else ""
        other_discount = discount_text == "other"
        try:
            wanted_discount = float(discount_text.rstrip("%")) if discount_text else None
        except ValueError:
            wanted_discount = None
        if not wanted and wanted_discount is None and not other_discount:
            return True

        def normalize_promo_type(value: Any) -> str:
            value = str(value or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")
            if value.startswith("chatgpt"):
                value = value[7:]
            if value.endswith("plan"):
                value = value[:-4]
            return value

        wanted = normalize_promo_type(wanted) if wanted not in {"*", "all", "any"} else ""
        for key, campaign in campaigns.items():
            metadata = campaign.get("metadata") if isinstance(campaign, dict) else {}
            plan_name = metadata.get("plan_name") if isinstance(metadata, dict) else ""
            type_matches = not wanted or wanted in {normalize_promo_type(key), normalize_promo_type(plan_name)}
            discount = metadata.get("discount") if isinstance(metadata, dict) else {}
            percentage = discount.get("percentage") if isinstance(discount, dict) else None
            try:
                if other_discount:
                    discount_matches = (
                        type(percentage) in (int, float)
                        and 0 < percentage <= 100 and percentage not in (50, 100)
                    )
                else:
                    discount_matches = wanted_discount is None or float(percentage) == wanted_discount
            except (TypeError, ValueError):
                discount_matches = False
            if type_matches and discount_matches:
                return True
        return False
    if f in {"free_no_trial", "free_without_trial", "free_not_trial"}:
        campaigns = row.get("eligible_promo_campaigns")
        return plan == "free" and isinstance(campaigns, dict) and not campaigns
    if f == "free":
        return plan == "free"
    return plan == f


def _decorate_outlook(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _outlook_line(out)
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_generic_api_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["code_url"] = _normalize_generic_api_code_url(out.get("code_url"))
    out["copy_line"] = _generic_api_email_line(out)
    out["password"] = out.get("password") or ""
    out["client_id"] = out.get("client_id") or ""
    out["refresh_token"] = out.get("refresh_token") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_imap_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _imap_email_line(out)
    account = account_by_email.get((out.get("email") or "").lower()) if account_by_email else None
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = ((account.get("access_token") or "")[:40] + "...") if account.get("access_token") else ""
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def list_email_pool_page(
    source: str = "all",
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """从统一邮箱库直接执行 COUNT + LIMIT/OFFSET。

    ``email_pool`` 是三个邮箱来源共用的表。source 为具体来源时按 id 倒序，
    source=all 时按入库时间合并倒序；两种情况都只从 SQLite 取当前页，
    不再先加载全部邮箱再由 WebUI 切片。
    """
    _ensure_sqlite()
    source = str(source or "outlook").strip().lower()
    # 新增邮箱来源时这里必须一起加，否则会静默回落成 outlook——
    # 表现是"筛选 akmoz 却返回一堆 outlook 邮箱"，很难看出是筛选没生效。
    if source not in {"all", "outlook", "generic_api", "imap", "akmoz",
                      "cloudflare_domain"}:
        source = "outlook"
    collection = "domain" if source == "cloudflare_domain" else source
    db_source = None if source == "all" else _EMAIL_SOURCES[collection]
    limit = max(1, int(limit))
    offset = max(0, int(offset or 0))
    where = ["1=1"]
    params: list[Any] = []
    if db_source is not None:
        where.append("ep.source=?")
        params.append(db_source)
    if status:
        where.append("ep.status=?")
        params.append(status)
    if q and str(q).strip():
        like = "%" + str(q).strip().lower() + "%"
        # payload 覆盖邮箱池自身字段；source 和关联账号 payload 保持旧 WebUI
        # 的搜索能力（例如搜索 generic_api 或已注册账号 token）。
        where.append(
            "(lower(ep.payload) LIKE ? OR lower(ep.source) LIKE ? OR EXISTS ("
            "SELECT 1 FROM accounts AS a "
            "WHERE a.email = ep.email COLLATE NOCASE AND lower(a.payload) LIKE ?))"
        )
        params.extend([like, like, like])
    clause = " AND ".join(where)
    order_by = "ep.created_at DESC, ep.id DESC" if source == "all" else "ep.id DESC"
    with _LOCK, closing(_sqlite_conn()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM email_pool AS ep WHERE {clause}", params).fetchone()[0])
        latest = str(conn.execute(
            f"SELECT COALESCE(MAX(ep.updated_at), '') FROM email_pool AS ep WHERE {clause}",
            params,
        ).fetchone()[0] or "")
        rows = conn.execute(
            f"SELECT ep.payload, ep.source, "
            f"(SELECT a.payload FROM accounts AS a "
            f" WHERE a.email = ep.email COLLATE NOCASE ORDER BY a.id DESC LIMIT 1) AS account_payload "
            f"FROM email_pool AS ep WHERE {clause} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

    # ``domain`` 是内部 collection 名，API 对外统一使用 cloudflare_domain；
    # 直接反转 _EMAIL_SOURCES 会把域名邮箱错误地返回成 source=domain。
    source_names = {
        _EMAIL_SOURCES["outlook"]: "outlook",
        _EMAIL_SOURCES["generic_api"]: "generic_api",
        _EMAIL_SOURCES["imap"]: "imap",
        _EMAIL_SOURCES["domain"]: "cloudflare_domain",
    }
    items: list[dict] = []
    for row in rows:
        item = json.loads(row["payload"])
        account_payload = row["account_payload"]
        account = None
        if account_payload:
            try:
                account = json.loads(account_payload)
            except (TypeError, ValueError):
                account = None
        item_source = source_names.get(str(row["source"]), str(row["source"]))
        if item_source == "outlook":
            item = _decorate_outlook(item, {str(item.get("email") or "").lower(): account} if account else {})
        elif item_source == "generic_api":
            item = _decorate_generic_api_email(item, {str(item.get("email") or "").lower(): account} if account else {})
        elif item_source == "imap":
            item = _decorate_imap_email(item, {str(item.get("email") or "").lower(): account} if account else {})
        else:
            item = dict(item)
        item["source"] = item_source
        if not item.get("copy_line"):
            item["copy_line"] = item.get("email") or ""
        items.append(item)
    return {"items": items, "total": total, "offset": offset, "limit": limit, "latest": latest}


def _get_conn() -> sqlite3.Connection:
    """兼容旧入口：返回 SQLite 连接。"""
    return _sqlite_conn()


def _row_to_dict(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


# ============================================================
# registered_accounts
# ============================================================

def insert_account(
    *,
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    plan_type: str | None = None,
    expires_at: str | None = None,
    device_id: str | None = None,
    proxy_used: str | None = None,
    proxy_region: str | None = None,
    email_source: str | None = None,
    extra: dict | None = None,
    codex_status: str | None = None,   # success / failed / skipped / missing
    codex_error: str | None = None,    # 失败原因（仅 codex_status=failed 时有意义）
) -> int:
    """插入或更新注册成功账号，返回本地文件中的 id。"""
    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        existing = _find_by_email(accounts, email)
        outlook_row = _find_by_email(outlook_rows, email)
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None

        if existing is None:
            row_id = _next_account_id()
            row = {
                "id": row_id,
                "email": email,
                "created_at": _now(),
            }
            accounts.append(row)
        else:
            row = existing
            row_id = int(row["id"])

        row.update({
            "access_token": access_token,
            "totp_secret": totp_secret if totp_secret is not None else row.get("totp_secret"),
            "user_id": user_id if user_id is not None else row.get("user_id"),
            "user_name": user_name if user_name is not None else row.get("user_name"),
            "plan_type": plan_type if plan_type is not None else row.get("plan_type"),
            "expires_at": expires_at if expires_at is not None else row.get("expires_at"),
            "proxy_used": proxy_used if proxy_used is not None else row.get("proxy_used"),
            # 出口地区标签（形如 1024·US）。1024proxy 的地址不在代理池里，
            # 界面按地址查名称查不到，没这个字段「注册代理」列只能显示 "-"，
            # 事后分不清账号是哪个地区注册的。
            "proxy_region": proxy_region if proxy_region else row.get("proxy_region"),
            "email_source": email_source if email_source is not None else row.get("email_source"),
            "extra_json": extra_json if extra_json is not None else row.get("extra_json"),
            "codex_status": codex_status if codex_status is not None else row.get("codex_status"),
            "codex_error": codex_error if codex_error is not None else row.get("codex_error"),
            "updated_at": _now(),
        })

        if outlook_row:
            row["password"] = outlook_row.get("password")
            row["client_id"] = outlook_row.get("client_id")
            row["refresh_token"] = outlook_row.get("refresh_token")
            row["original_email_line"] = _outlook_line(outlook_row)
            outlook_row["status"] = "used"
            outlook_row["used_at"] = outlook_row.get("used_at") or _now()
            outlook_row["registered_account_id"] = row_id
            outlook_row["access_token"] = access_token
            outlook_row["completed_at"] = _now()
            if totp_secret:
                outlook_row["totp_secret"] = totp_secret

        row["copy_line"] = _account_line(row)
        _save_accounts(accounts)
        _save_outlook(outlook_rows)
        return row_id


def update_account_codex_status(email: str, codex_status: str, codex_error: str | None = None) -> bool:
    """
    单独更新某账号的 codex_status / codex_error（手动补跑 Codex 时用）。
    返回是否找到该账号。
    """
    with _LOCK:
        accounts = _load_accounts()
        row = _find_by_email(accounts, email)
        if row is None:
            return False
        row["codex_status"] = codex_status
        row["codex_error"] = codex_error
        if str(codex_status or "").strip().lower() == "deactivated":
            # Codex 授权阶段判定为 deactivated，按账号废号处理，便于账号列表统一筛选。
            row["live_check_status"] = "deactivated"
            row["live_check_ok"] = False
            row["live_check_error"] = codex_error or "Codex 授权判定账号已废号"
            row["live_checked_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_codex_agent(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 Codex Agent Token 生成任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("codex_agent_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "codex_agent_queued_at" if current_status == "queued" else "codex_agent_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["codex_agent_status"] = "queued"
        row["codex_agent_ok"] = False
        row["codex_agent_trigger"] = str(trigger or "manual")
        row["codex_agent_queued_at"] = now
        row["codex_agent_started_at"] = None
        row["codex_agent_completed_at"] = None
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_codex_agent_running(acc_id: int) -> bool:
    """把 Codex Agent Token 生成任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("codex_agent_status") not in {"queued", "running"}:
            return False
        row["codex_agent_status"] = "running"
        row["codex_agent_started_at"] = _now()
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "正在生成 Codex Agent Token"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_codex_agent(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 Codex Agent Token 生成结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["codex_agent_status"] = status
        row["codex_agent_ok"] = ok
        row["codex_agent_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["codex_agent_completed_at"] = _now()
        row["codex_agent_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["codex_agent_message"] = result.get("message")
        if result.get("agent_runtime_id") is not None:
            row["codex_agent_runtime_id"] = result.get("agent_runtime_id")
        if result.get("auth_path") is not None:
            row["codex_agent_auth_path"] = result.get("auth_path")
        if isinstance(result.get("auth_json"), dict):
            auth_json = result.get("auth_json")
            row["codex_agent_token"] = json.dumps(auth_json, ensure_ascii=False)
            agent_filename = f"codex-agent-{str(row.get('email') or acc_id)}.json"
            stamp = _now()
            _ensure_sqlite()
            with closing(_sqlite_conn()) as conn:
                conn.execute(
                    "INSERT INTO codex_agent_accounts(account_id,email,filename,created_at,updated_at,payload) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET email=excluded.email, filename=excluded.filename, updated_at=excluded.updated_at, payload=excluded.payload",
                    (int(acc_id), str(row.get("email") or ""), agent_filename, stamp, stamp, json.dumps(auth_json, ensure_ascii=False)),
                )
                conn.commit()
            row.pop("codex_agent_auth_path", None)
        for _k in (
            "codex_agent_network_route",
            "codex_agent_proxy_mode",
            "codex_agent_proxy_used",
            "codex_agent_proxy_fallback_reason",
            "codex_agent_attempt_count",
            "codex_agent_max_attempts",
            "codex_agent_request_timeout",
            "codex_agent_sub2api_path",
            "codex_agent_sub2api_url",
            "codex_agent_sub2api_mode",
            "codex_agent_sub2api_total",
        ):
            src_key = _k.replace("codex_agent_", "", 1)
            if result.get(src_key) is not None:
                row[_k] = result.get(src_key)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def get_codex_agent_credential(acc_id: int) -> tuple[str, str] | None:
    """从 SQLite 获取 Agent 凭证，返回 JSON 文本和下载文件名。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        row = conn.execute("SELECT filename, payload FROM codex_agent_accounts WHERE account_id=?", (int(acc_id),)).fetchone()
    if not row:
        return None
    return json.dumps(json.loads(row["payload"]), ensure_ascii=False, indent=2) + "\n", row["filename"]


def recover_interrupted_codex_agents() -> int:
    """服务启动时恢复上次进程中断的 Codex Agent 任务状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("codex_agent_status") not in {"queued", "running"}:
                continue
            row["codex_agent_status"] = "failed"
            row["codex_agent_ok"] = False
            row["codex_agent_error"] = "WebUI 重启导致 Codex Agent Token 任务中断，请重新生成"
            row["codex_agent_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_plan_check(
    acc_id: int | None = None,
    email: str | None = None,
    trigger: str = "manual",
    *, run_id: str | None = None,
) -> bool:
    """原子占用账号的套餐查询；已有未超时查询时返回 False。"""
    with _LOCK, _account_for_update(acc_id, email) as row:
        if row is None:
            return False

        current_status = row.get("plan_check_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "plan_check_queued_at" if current_status == "queued" else "plan_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = _now()
        row["plan_check_status"] = "queued"
        row["plan_check_run_id"] = run_id
        row["plan_check_trigger"] = str(trigger or "manual")
        row["plan_check_queued_at"] = now
        row["plan_check_started_at"] = None
        row["plan_check_completed_at"] = None
        row["plan_check_error"] = None
        row["updated_at"] = now
        return True


def mark_account_plan_check_running(acc_id: int, *, run_id: str | None = None, expected_account: dict | None = None) -> bool:
    """把已排队的套餐查询标记为执行中。"""
    with _LOCK, _account_for_update(acc_id) as row:
        if row is None or row.get("plan_check_status") not in {"queued", "running"}:
            return False
        if run_id is not None and row.get("plan_check_run_id") not in (None, run_id):
            return False
        if not _matches_check_snapshot(row, expected_account):
            row.update(plan_check_status="failed", plan_check_ok=False,
                       plan_check_error="账号邮箱/AT 已变更，旧套餐任务已取消",
                       plan_check_completed_at=_now(), updated_at=_now())
            return False
        row["plan_check_status"] = "running"
        row["plan_check_run_id"] = run_id
        row["plan_check_started_at"] = _now()
        row["plan_check_error"] = None
        row["updated_at"] = _now()
        return True


def recover_interrupted_plan_checks() -> int:
    """服务启动时把上次进程遗留的内存队列状态恢复为可重试失败。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("plan_check_status") not in {"queued", "running"}:
                continue
            row["plan_check_status"] = "failed"
            row["plan_check_ok"] = False
            row["plan_check_error"] = "WebUI 重启导致套餐查询中断，请重新查询"
            row["plan_check_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def _matches_check_snapshot(row: dict, expected: dict | None) -> bool:
    """网络请求期间账号身份/AT 变化时，禁止旧结果覆盖新账号状态。调用方持有 _LOCK。"""
    if expected is None:
        return True
    for key in ("email", "access_token"):
        if key not in expected:
            continue
        current, previous = str(row.get(key) or ""), str(expected.get(key) or "")
        if key == "email":
            current, previous = current.strip().lower(), previous.strip().lower()
        if current != previous:
            return False
    return True


def update_account_plan_check(acc_id: int | None = None, email: str | None = None, result: dict | None = None, *, expected_account: dict | None = None, expected_run_id: str | None = None) -> bool:
    """更新账号套餐/Plus 试用资格查询结果。"""
    result = result or {}
    with _LOCK, _account_for_update(acc_id, email) as row:
        if row is None:
            return False
        if expected_run_id is not None:
            allowed_statuses = {"running"} if result.get("ok") else {"queued", "running"}
            if row.get("plan_check_run_id") != expected_run_id or row.get("plan_check_status") not in allowed_statuses:
                return False
        if not _matches_check_snapshot(row, expected_account):
            # 只结束自己占用的旧查询，不动后续任务，也不覆盖已有套餐权益。
            if expected_run_id is not None:
                row.update(plan_check_status="failed", plan_check_ok=False,
                           plan_check_error="账号邮箱/AT 已变更，旧套餐结果已丢弃",
                           plan_check_completed_at=_now(), updated_at=_now())
            return False

        ok = bool(result.get("ok"))
        row["plan_check_status"] = "success" if ok else "failed"
        row["plan_check_ok"] = ok
        row["plan_checked_at"] = result.get("checked_at") or _now()
        row["plan_check_completed_at"] = _now()
        row["plan_check_http_status"] = result.get("http_status")
        row["plan_check_error"] = None if ok else result.get("error")

        if result.get("account_id"):
            row["account_id"] = result.get("account_id")
        # 查询失败只更新本次错误和网络信息，不覆盖上一次成功拿到的套餐、
        # 试用资格、优惠及有效期，避免临时网络故障把真实权益清空。
        if ok:
            if result.get("current_plan_type"):
                row["current_plan_type"] = result.get("current_plan_type")
                row["plan_type"] = result.get("current_plan_type")
            if result.get("subscription_plan") is not None:
                row["subscription_plan"] = result.get("subscription_plan")
            if result.get("has_active_subscription") is not None:
                row["has_active_subscription"] = bool(result.get("has_active_subscription"))
            if result.get("expires_at") is not None:
                row["plan_expires_at"] = result.get("expires_at")
            if result.get("renews_at") is not None:
                row["plan_renews_at"] = result.get("renews_at")
            if result.get("cancels_at") is not None:
                row["plan_cancels_at"] = result.get("cancels_at")
            if result.get("billing_period") is not None:
                row["billing_period"] = result.get("billing_period")
            if result.get("billing_currency") is not None:
                row["billing_currency"] = result.get("billing_currency")
            if result.get("is_delinquent") is not None:
                row["is_delinquent"] = bool(result.get("is_delinquent"))
            for _k in (
                "discount_type",
                "discount_amount",
                "discount_duration_num_periods",
                "discount_expires_at",
                "discount_cancellation_policy",
                "discount_promo_campaign_id",
                "last_purchase_origin_platform",
                "last_will_renew",
            ):
                if result.get(_k) is not None:
                    row[_k] = result.get(_k)

            row["plus_trial_eligible"] = bool(result.get("plus_trial_eligible"))
            row["plus_trial_campaign_id"] = result.get("plus_trial_campaign_id")
            row["plus_trial_title"] = result.get("plus_trial_title")
            row["plus_trial_discount_percentage"] = result.get("plus_trial_discount_percentage")
            row["plus_trial_duration_num_periods"] = result.get("plus_trial_duration_num_periods")
            row["plus_trial_duration_period"] = result.get("plus_trial_duration_period")
            row["eligible_offer_ids"] = result.get("eligible_offer_ids") or []
            row["eligible_promo_campaigns"] = result.get("eligible_promo_campaigns") or {}
            row["workspace_plans"] = result.get("workspace_plans") or []
            row["personal_plan_type"] = result.get("personal_plan_type") or ""
            row["paid_workspace_account_id"] = result.get("paid_workspace_account_id")
            row["paid_workspace_name"] = result.get("paid_workspace_name")
            row["limit_windows"] = result.get("limit_windows") or []
            row["limit_features"] = result.get("limit_features") or []
            row["limit_checked_at"] = result.get("limit_checked_at")
            # 套餐限额：Codex/Work 共享的那份，和上面的聊天额度不是一回事
            row["plan_limit_windows"] = result.get("plan_limit_windows") or []
            row["plan_limit_detail"] = result.get("plan_limit_detail") or []
            row["plan_limit_reached"] = result.get("plan_limit_reached")
            row["plan_limit_checked_at"] = result.get("plan_limit_checked_at")
            row["plan_last_success_at"] = result.get("checked_at") or _now()
            row["plan_last_success_result_json"] = json.dumps(result, ensure_ascii=False)
        row["plan_check_proxy_mode"] = result.get("proxy_mode")
        row["plan_check_network_route"] = result.get("network_route")
        row["plan_check_proxy_used"] = result.get("proxy_used")
        row["plan_check_proxy_fallback_reason"] = result.get("proxy_fallback_reason")
        for key in ("token_expired", "token_expires_at"):
            if key in result:
                row[key] = result[key]
        row["plan_check_result_json"] = json.dumps(result, ensure_ascii=False)
        row["updated_at"] = _now()
        needs_no = bool(row.get("paid_at")) and not row.get("subscription_no")
        target_id = int(row.get("id") or 0)

    # 限额类型到套餐查完才确定，订阅编号在这里补发。
    # 放在 with 外面：assign_subscription_no 自己要拿 _LOCK，在里面调会重入。
    if needs_no and target_id:
        try:
            assign_subscription_no(target_id)
        except Exception:  # noqa: BLE001 - 编号失败不能影响套餐结果
            logger.exception("[DB] 分配订阅编号失败: acc_id=%s", target_id)
    return True


def claim_account_extract(acc_id: int, trigger: str = "manual", link_type: str = "pix") -> bool:
    """原子占用账号提链任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("extract_link_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "extract_link_queued_at" if current_status == "queued" else "extract_link_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["extract_link_status"] = "queued"
        row["extract_link_ok"] = False
        row["extract_link_trigger"] = str(trigger or "manual")
        row["extract_link_type"] = str(link_type or "pix").lower()
        row["extract_link_queued_at"] = now
        row["extract_link_started_at"] = None
        row["extract_link_completed_at"] = None
        row["extract_link_error"] = None
        row["extract_link_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_extract_running(acc_id: int) -> bool:
    """把提链任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("extract_link_status") not in {"queued", "running"}:
            return False
        row["extract_link_status"] = "running"
        row["extract_link_started_at"] = _now()
        row["extract_link_error"] = None
        row["extract_link_message"] = "任务运行中"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_extract(acc_id: int, result: dict | None = None) -> bool:
    """更新账号提链任务结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["extract_link_status"] = status
        row["extract_link_ok"] = ok
        row["extract_link_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["extract_link_completed_at"] = _now()
        row["extract_link_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["extract_link_message"] = result.get("message")
        if result.get("job_id") is not None:
            row["extract_link_job_id"] = result.get("job_id")
        if result.get("link_type") is not None:
            row["extract_link_type"] = result.get("link_type")
        if result.get("cdk_remaining") is not None:
            row["extract_link_cdk_remaining"] = result.get("cdk_remaining")
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        if payload:
            row["extract_link_long_url"] = payload.get("long_url")
            row["extract_link_copy_paste"] = payload.get("copy_paste")
            row["extract_link_image_url_png"] = payload.get("image_url_png")
            row["extract_link_image_url_svg"] = payload.get("image_url_svg")
            row["extract_link_payment_method"] = payload.get("payment_method")
            row["extract_link_payment_link_type"] = payload.get("payment_link_type")
            row["extract_link_expires_at"] = payload.get("expires_at")
            if payload.get("cdk_remaining") is not None:
                row["extract_link_cdk_remaining"] = payload.get("cdk_remaining")
            row["extract_link_result_json"] = json.dumps(payload, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_extract_links() -> int:
    """服务启动时恢复上次进程中断的提链状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("extract_link_status") not in {"queued", "running"}:
                continue
            row["extract_link_status"] = "failed"
            row["extract_link_ok"] = False
            row["extract_link_error"] = "WebUI 重启导致提链任务中断，请重新提链"
            row["extract_link_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def _account_matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _parse_iso_dt(value: str | None, end_of_day: bool = False) -> datetime | None:
    """宽松解析 ISO 日期/时间字符串；支持 YYYY-MM-DD 或完整 ISO；解析失败返回 None。

    end_of_day=True 时，纯日期（YYYY-MM-DD）按当天 23:59:59.999999 解析，
    用于 date_to 过滤（保证包含截止当天）；完整时间串原样返回。
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) == 10 and text[4] == "-":
            if end_of_day:
                return datetime.fromisoformat(text + "T23:59:59.999999")
            return datetime.fromisoformat(text + "T00:00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _matches_codex_status_filter(row: dict, codex_filter: str | None) -> bool:
    codex_filter = str(codex_filter or "").strip().lower()
    if not codex_filter:
        return True
    status = str(row.get("codex_status") or "").strip().lower()
    live_status = str(row.get("live_check_status") or "").strip().lower()
    if codex_filter in {"all", "*"}:
        return True
    if codex_filter == "deactivated":
        return live_status == "deactivated"
    return status == codex_filter


def _matches_totp_status_filter(row: dict, totp_filter: str | None) -> bool:
    """按 2FA/TOTP 是否已配置及设置任务状态筛选账号。"""
    totp_filter = str(totp_filter or "").strip().lower()
    if not totp_filter or totp_filter in {"all", "*"}:
        return True

    enabled = bool(str(row.get("totp_secret") or "").strip())
    setup_status = str(row.get("totp_setup_status") or "").strip().lower()
    if totp_filter in {"enabled", "on", "active"}:
        return enabled
    if totp_filter in {"disabled", "off", "not_enabled", "unset"}:
        return not enabled
    if totp_filter in {"pending", "setup", "setting", "queued", "running"}:
        return setup_status in {"queued", "running"}
    if totp_filter in {"failed", "stopped"}:
        return setup_status == totp_filter
    return setup_status == totp_filter


def _matches_seat_filter(row: dict, seat_filter: str | None) -> bool:
    """按单席位探测结果筛选账号。

    single_seat_supported 是三态：
      True  -> 确定支持 1 席
      False -> 确定要 2 席（订阅要多花一倍钱）
      缺失  -> 还没探测过

    "过滤2席位"（not_multi）只滤掉确定为 False 的那批；没探过的必须留下，
    实测它们里约 55% 是单席位，一并滤掉会把大半可用号误伤。
    """
    seat_filter = str(seat_filter or "").strip().lower()
    if not seat_filter or seat_filter in {"all", "*"}:
        return True

    sup = row.get("single_seat_supported")
    # 用真值判断而不是 `is False`：SQLite 里布尔存成 0/1，JSON 回来可能是
    # False 也可能是 0。`is False` 认不出 0，会和 SQL 侧的 `!= 0` 判定分歧，
    # 导致分页接口和非分页接口返回不同的行（自己的测试抓到过这个）。
    probed = sup is not None
    is_multi = probed and not bool(sup)
    is_single = probed and bool(sup)

    if seat_filter in {"not_multi", "hide_multi", "exclude_multi"}:
        return not is_multi
    if seat_filter in {"single", "single_only", "one"}:
        return is_single
    if seat_filter in {"multi", "multi_only", "two"}:
        return is_multi
    if seat_filter in {"unknown", "unprobed", "not_checked"}:
        return not probed
    if seat_filter in {"free_trial", "free", "team_free_trial"}:
        return bool(row.get("team_free_trial"))
    if seat_filter in {"discount", "has_discount"}:
        return (
            bool(row.get("team_free_trial"))
            or float(row.get("team_checkout_discount_pct") or 0) > 0
            or int(row.get("team_checkout_discount_minor") or 0) > 0
        )
    return True


def _filtered_decorated_accounts(
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    seat_filter: str | None = None,
) -> list[dict]:
    rows = _load_accounts()
    if archived in (True, "1", "true", "yes", "only"):
        rows = [r for r in rows if bool(r.get("archived"))]
    elif archived in ("all", "include"):
        pass
    else:
        rows = [r for r in rows if not bool(r.get("archived"))]
    decorated = [_decorate_account(r) for r in rows]
    decorated = [r for r in decorated if _account_matches_plan_filter(r, plan_filter)]
    decorated = [r for r in decorated if _matches_codex_status_filter(r, codex_filter)]
    decorated = [r for r in decorated if _matches_totp_status_filter(r, totp_filter)]
    decorated = [r for r in decorated if _matches_seat_filter(r, seat_filter)]
    decorated = [r for r in decorated if _account_matches_query(r, q)]
    # 按创建时间筛选（date_from/date_to 为 ISO 字符串或 YYYY-MM-DD）
    if date_from or date_to:
        d_from = _parse_iso_dt(date_from)
        d_to = _parse_iso_dt(date_to, end_of_day=True)
        if d_from or d_to:
            filtered = []
            for r in decorated:
                ct = _parse_iso_dt(str(r.get("created_at") or ""))
                if ct is None:
                    continue
                if d_from and ct < d_from:
                    continue
                if d_to and ct > d_to:
                    continue
                filtered.append(r)
            decorated = filtered
    return sorted(decorated, key=lambda x: int(x.get("id") or 0), reverse=True)


def list_account_plan_check_statuses(
    limit: int = 5000,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    seat_filter: str | None = None,
) -> dict:
    """返回不含 Token/邮箱密码的套餐查询轻量状态快照。"""
    fields = (
        "id", "email", "archived",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "eligible_promo_campaigns", "plus_trial_discount_percentage",
        "plan_check_status", "plan_check_ok", "plan_check_error",
        "plan_check_trigger", "plan_check_queued_at", "plan_check_started_at",
        "plan_check_completed_at", "plan_checked_at", "plan_last_success_at",
        "plan_check_network_route", "plan_check_proxy_used", "plan_check_proxy_fallback_reason",
        "workspace_plans", "personal_plan_type", "paid_workspace_account_id",
        "paid_workspace_name", "account_id", "limit_windows", "limit_checked_at",
        "limit_features",
        "plan_limit_windows", "plan_limit_detail", "plan_limit_reached",
        "plan_limit_checked_at",
        "subscription_no", "subscription_kind", "paid_at", "paid_at_estimated",
        "live_check_proxy_used", "live_check_fingerprint_text",
        "expires_at", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "extract_link_status", "extract_link_ok", "extract_link_type",
        "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste",
        "extract_link_image_url_png", "extract_link_image_url_svg",
        "extract_link_expires_at",
        "codex_status", "codex_error",
        "codex_agent_status", "codex_agent_message",
        "codex_agent_runtime_id", "codex_agent_sub2api_url",
        "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
        "totp_setup_status", "totp_setup_ok", "totp_setup_error",
        "totp_setup_message", "totp_setup_trigger", "totp_setup_queued_at",
        "totp_setup_started_at", "totp_setup_completed_at", "totp_setup_checked_at",
        "password_backfill_status", "password_backfill_error", "password_backfill_message",
        "card_rebind_status", "card_rebind_error", "card_rebind_message", "card_rebind_card_label",
        "original_email", "email_source", "email_change_status", "email_change_ok",
        "email_change_error", "email_change_new_email", "email_change_started_at", "email_change_completed_at",
        "single_seat_supported", "single_seat_minimum",
        "single_seat_checked_at", "single_seat_error",
        "team_free_trial", "team_checkout_total_minor",
        "team_checkout_total_amount", "team_checkout_currency",
        "team_checkout_subtotal_minor", "team_checkout_subtotal_amount",
        "team_checkout_discount_minor", "team_checkout_discount_amount",
        "team_checkout_discount_pct", "team_checkout_discount_label",
    )
    with _LOCK:
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        extra_where, extra_params = _account_filter_sql(
            plan_filter=plan_filter,
            codex_filter=codex_filter,
            totp_filter=totp_filter,
            seat_filter=seat_filter,
        )
        candidates, total, latest = _query_collection_page(
            "accounts",
            archived=archived,
            q=q,
            date_from=date_from,
            date_to=date_to,
            extra_where=extra_where,
            extra_params=extra_params,
            order_by=_ACCOUNTS_ORDER_BY,
            limit=limit,
            offset=offset,
        )
        rows = [_decorate_account(row) for row in candidates]
        items = []
        for row in rows:
            item = {"id": row.get("id"), "email": row.get("email")}
            for key in fields:
                value = row.get(key)
                if key in ("id", "email"):
                    continue
                if value is not None and value != "":
                    item[key] = value
            item["totp_enabled"] = bool(str(row.get("totp_secret") or "").strip())
            plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
            if not any(x in plan for x in ("plus", "pro", "team", "go")):
                for expire_key in ("expires_at", "plan_expires_at", "plan_renews_at", "renews_at"):
                    item.pop(expire_key, None)
            item["codex_agent_has_token"] = bool(str(row.get("codex_agent_token") or "").strip())
            item["has_access_token"] = bool(str(row.get("access_token") or "").strip())
            items.append(item)
        # updated_at 目前只有秒级精度；一次快速查询可能在同一秒内完成
        # queued -> running -> success/failed，导致 revision 不变，前端跳过合并状态，
        # 页面就会一直停在“查询中”。把轻量状态本身纳入签名，保证状态变化可被轮询发现。
        revision_payload = json.dumps(
            [
                {
                    "id": row.get("id"),
                    "updated_at": row.get("updated_at"),
                    "plan_check_status": row.get("plan_check_status"),
                    "plan_check_ok": row.get("plan_check_ok"),
                    "plan_check_error": row.get("plan_check_error"),
                    "current_plan_type": row.get("current_plan_type"),
                    "plan_type": row.get("plan_type"),
                    "plus_trial_eligible": row.get("plus_trial_eligible"),
                    "eligible_promo_campaigns": row.get("eligible_promo_campaigns"),
                    "extract_link_status": row.get("extract_link_status"),
                    "codex_status": row.get("codex_status"),
                    "codex_agent_status": row.get("codex_agent_status"),
                    "totp_setup_status": row.get("totp_setup_status"),
                    "totp_setup_ok": row.get("totp_setup_ok"),
                    "totp_setup_error": row.get("totp_setup_error"),
                    "totp_setup_message": row.get("totp_setup_message"),
                    "totp_setup_checked_at": row.get("totp_setup_checked_at"),
                    "totp_setup_started_at": row.get("totp_setup_started_at"),
                    "totp_setup_completed_at": row.get("totp_setup_completed_at"),
                    "totp_enabled": bool(str(row.get("totp_secret") or "").strip()),
                    "email": row.get("email"),
                    "original_email": row.get("original_email"),
                    "email_source": row.get("email_source"),
                    "email_change_status": row.get("email_change_status"),
                    "email_change_error": row.get("email_change_error"),
                }
                for row in rows
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        revision_sig = hashlib.sha1(revision_payload.encode("utf-8")).hexdigest()[:12]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}:{revision_sig}"}


def list_accounts(
    limit: int = 500,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    seat_filter: str | None = None,
) -> list[dict]:
    # 非分页兼容接口也走同一条 SQL 分页路径，避免 limit=500 时先读取整张表。
    result = list_accounts_page(
        limit=limit,
        offset=offset,
        archived=archived,
        plan_filter=plan_filter,
        codex_filter=codex_filter,
        q=q,
        date_from=date_from,
        date_to=date_to,
        totp_filter=totp_filter,
        seat_filter=seat_filter,
    )
    return result["items"]


def list_accounts_page(
    limit: int = 50,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    totp_filter: str | None = None,
    seat_filter: str | None = None,
) -> dict:
    with _LOCK:
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        extra_where, extra_params = _account_filter_sql(
            plan_filter=plan_filter,
            codex_filter=codex_filter,
            totp_filter=totp_filter,
            seat_filter=seat_filter,
        )
        candidates, total, latest = _query_collection_page(
            "accounts",
            archived=archived,
            q=q,
            date_from=date_from,
            date_to=date_to,
            extra_where=extra_where,
            extra_params=extra_params,
            order_by=_ACCOUNTS_ORDER_BY,
            limit=limit,
            offset=offset,
        )
        items = [_decorate_account(row) for row in candidates]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}"}


def get_account(acc_id: int) -> dict | None:
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = _select_account(conn, acc_id)
        return _decorate_account(row) if row else None


def get_account_by_email(email: str) -> dict | None:
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            if not email:
                # 保留历史空邮箱匹配行为；其他 API 的空邮箱参数不代表要选择账号。
                record = conn.execute("SELECT payload FROM accounts WHERE email='' ORDER BY id LIMIT 1").fetchone()
                row = json.loads(record["payload"]) if record else None
            else:
                row = _select_account(conn, email=email)
        return _decorate_account(row) if row else None


def set_account_paid_at(acc_id: int, when: str | None = None) -> bool:
    """记录支付成功时间。已有值时不覆盖，避免重复支付把首次时间冲掉。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("paid_at"):
            return False
        row["paid_at"] = str(when or _now())
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def set_account_pay_card(acc_id: int, label: str, *, force: bool = False) -> bool:
    """记录这个账号的订阅是哪张卡付的。

    label 形如 VCC-1771（VCC 托管卡）或卡池备注名（自有卡）。
    不存卡号、不存卡 ID：对账和排查只需要能认出是哪张，存更多只会
    让账号表多一份敏感信息。

    已有值时不覆盖：首次支付用哪张卡才是要查的，后续补登或重复调用
    不该把它冲掉（和 set_account_paid_at 保持一致的语义）。
    换绑卡成功后用 force=True 覆盖——换绑后的扣款卡才是对账要看的。
    """
    label = str(label or "").strip()
    if not label:
        return False
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or (str(row.get("pay_card_label") or "").strip() and not force):
            return False
        row["pay_card_label"] = label[:64]
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def clear_account_payment(acc_id: int) -> bool:
    """回滚"已支付"登记：清掉 paid_at 与支付卡标签。

    仅用于套餐确认失败的补偿（2026-11-19 #150：页面无报错被当成支付
    成功，实际未扣款未生效）。有值才清，无值返回 False，幂等可重入。
    """
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        changed = False
        for key in ("paid_at", "pay_card_label"):
            if str(row.get(key) or "").strip():
                row[key] = ""
                changed = True
        if not changed:
            return False
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


# 两种限额各自独立编号，起点由历史数据定：5h 从 49 开始（id 1~8 那批
# 已删除的账号占了 41~48），周限额从 8 开始。
_SUB_NO_START = {"5h": 49, "weekly": 8}


def _limit_kind(row: dict) -> str:
    """付费账号属于哪种限额。只有 weekly 一种窗口才算周限额。"""
    wins = row.get("plan_limit_windows") or []
    if not isinstance(wins, list) or not wins:
        return ""
    return "weekly" if wins == ["weekly"] else ("5h" if "5h" in wins else "")


def assign_subscription_no(acc_id: int) -> int | None:
    """给账号分配订阅编号，按限额类型分两条序列各自递增。

    已有编号直接返回，不重复分配。限额类型还没查出来时返回 None，
    等套餐查询回来再调一次。

    取 max+1 而不是最小未用号：编号要能反映付款先后。手工调整编号留下的
    空位如果被后来的账号填掉，就会出现"11-13 付款的账号编号排在 11-03
    前面"这种错乱（线上发生过：#69 付款最晚却拿到了空出来的 67）。

    编号按 paid_at 先后排定，不按调用顺序：本函数在套餐查询返回时被调用，
    而套餐查询是并发的——先付款的账号可能后返回，于是拿到更大的编号
    （2026-11-18 线上：#88 付款 04:32 拿到 85，#90 付款 05:41 拿到 84）。
    所以这里要检查是否有「付款更晚但已拿到编号」的同类账号，有就和它换位，
    保证编号顺序始终等于付款顺序。
    """
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return None
        existing = row.get("subscription_no")
        if existing:
            return int(existing)
        kind = _limit_kind(row)
        if not kind:
            return None
        used = {
            int(r["subscription_no"])
            for r in rows
            if r.get("subscription_no") and _limit_kind(r) == kind
        }
        start = _SUB_NO_START.get(kind, 1)
        nxt = max(used) + 1 if used else start
        if nxt < start:
            nxt = start
        row["subscription_no"] = nxt
        row["subscription_kind"] = kind
        row["updated_at"] = _now()

        # 和付款更晚却已编号的同类账号换位，让编号顺序对齐付款顺序。
        mine_paid = str(row.get("paid_at") or "")
        if mine_paid:
            later = [
                r for r in rows
                if r is not row and r.get("subscription_no")
                and _limit_kind(r) == kind
                and str(r.get("paid_at") or "") > mine_paid
            ]
            # 从编号最小的那个开始换：它本该排在本账号后面
            for other in sorted(later, key=lambda r: int(r["subscription_no"])):
                o_no = int(other["subscription_no"])
                if o_no < nxt:
                    other["subscription_no"], row["subscription_no"] = nxt, o_no
                    other["updated_at"] = _now()
                    logger.info(
                        "[DB] 订阅编号按付款时间换位：#%s(%s) 取 %s，"
                        "#%s(%s) 改为 %s",
                        acc_id, mine_paid[:19], o_no,
                        other.get("id"), str(other.get("paid_at"))[:19], nxt)
                    nxt = o_no
                    break

        _save_accounts(rows)
        return int(row["subscription_no"])


def update_account_access_token(acc_id: int, access_token: str) -> bool:
    """覆盖账号的 accessToken。

    2FA 的重认证会作废注册时那个 token，换到的新 token 必须写回来，否则存档
    里留的是一个已失效的 token，查活/查套餐都会拿到 401 token_revoked。
    """
    token = str(access_token or "").strip()
    if not token:
        return False
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("access_token") == token:
            return False
        row["access_token"] = token
        row["access_token_updated_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def update_account_note(acc_id: int, note: str) -> bool:
    """更新单个已注册账号备注。note 为空字符串时表示清空备注。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["note"] = str(note or "")
        row["note_updated_at"] = now
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def claim_account_email_change(acc_id: int, source: str, trigger: str = "manual") -> bool:
    """原子占用账号邮箱换绑任务。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("email_change_status") in {"queued", "running"}:
            return False
        now = _now()
        row.update({
            "email_change_status": "queued", "email_change_ok": False,
            "email_change_source": str(source or ""), "email_change_trigger": str(trigger or "manual"),
            "email_change_queued_at": now, "email_change_started_at": None,
            "email_change_completed_at": None, "email_change_error": None, "updated_at": now,
        })
        _save_accounts(rows)
        return True


def mark_account_email_change_running(acc_id: int, new_email: str) -> bool:
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("email_change_status") not in {"queued", "running"}:
            return False
        row.update({"email_change_status": "running", "email_change_new_email": new_email,
                    "email_change_started_at": _now(), "email_change_error": None, "updated_at": _now()})
        _save_accounts(rows)
        return True


def finish_account_email_change(
    acc_id: int, *, ok: bool, new_email: str | None = None, source: str | None = None,
    material_line: str | None = None, error: str | None = None,
) -> bool:
    """写回换绑结果；成功时保留初始邮箱并将账号主邮箱切换为新邮箱。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        if ok and new_email:
            old_email = str(row.get("email") or "").strip()
            row["original_email"] = str(row.get("original_email") or old_email)
            history = row.get("email_history") if isinstance(row.get("email_history"), list) else []
            if old_email and old_email.lower() not in {str(x).lower() for x in history}:
                history.append(old_email)
            row["email_history"] = history
            row["email"] = str(new_email).strip()
            row["email_source"] = str(source or row.get("email_source") or "")
            row["original_email_line"] = str(material_line or new_email)
            # 清理旧邮箱来源遗留的 Outlook 凭证；若新来源仍为 Outlook 则写入新素材。
            row["password"] = ""
            row["client_id"] = ""
            row["refresh_token"] = ""
            if str(source or "") == "outlook":
                mailbox = _find_by_email(_load_outlook(), str(new_email))
                if mailbox:
                    row["password"] = mailbox.get("password") or ""
                    row["client_id"] = mailbox.get("client_id") or ""
                    row["refresh_token"] = mailbox.get("refresh_token") or ""
            # 抓包表明 verify 成功后当前 OAuth token 会立即失效。
            row["access_token"] = ""
            row["token_expired"] = True
            row["live_check_status"] = ""
            row["live_check_run_id"] = None
            row["email_change_new_email"] = str(new_email).strip()
        row["email_change_status"] = "success" if ok else "failed"
        row["email_change_ok"] = bool(ok)
        row["email_change_error"] = None if ok else str(error or "换绑失败")[:1000]
        row["email_change_completed_at"] = now
        row["updated_at"] = now
        row["copy_line"] = _account_line(row)
        _save_accounts(rows)
        return True


def recover_interrupted_email_changes() -> int:
    """启动时将上次进程中断的邮箱换绑任务标记为失败。"""
    with _LOCK:
        rows = _load_accounts()
        count = 0
        for row in rows:
            if row.get("email_change_status") not in {"queued", "running"}:
                continue
            row.update({
                "email_change_status": "failed", "email_change_ok": False,
                "email_change_error": "WebUI 重启导致邮箱换绑中断，请重新操作",
                "email_change_completed_at": _now(), "updated_at": _now(),
            })
            count += 1
        if count:
            _save_accounts(rows)
        return count


def update_account_liveness(acc_id: int, result: dict | None = None, *, expected_account: dict | None = None, expected_run_id: str | None = None) -> bool:
    """写回账号查活结果；成功时同步刷新最新 access_token 和账号基础信息。"""
    result = result or {}
    with _LOCK, _account_for_update(acc_id) as row:
        if row is None:
            return False
        if expected_run_id is not None and (row.get("live_check_run_id") != expected_run_id
                                            or row.get("live_check_status") not in {"queued", "running"}):
            return False
        if not _matches_check_snapshot(row, expected_account):
            if expected_run_id is not None:
                row.update(live_check_status="failed", live_check_ok=False, live_checked_at=_now(),
                           live_check_error="账号邮箱/AT 已变更，旧查活结果已丢弃", updated_at=_now())
            return False

        now = _now()
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("live" if ok else "failed"))
        row["live_check_status"] = status
        row["live_check_ok"] = ok
        row["live_checked_at"] = result.get("checked_at") or now
        row["live_check_error"] = None if ok else result.get("error")
        row["updated_at"] = now

        if ok:
            token = str(result.get("access_token") or "").strip()
            if token:
                row["access_token"] = token
                # token 元数据属于当前 AT，不能等到套餐成功才清理旧 AT 的过期标记。
                try:
                    from core.chatgpt_plan import token_claims
                    claims = token_claims(token)
                except (TypeError, ValueError, AttributeError, OverflowError, OSError):
                    claims = {}
                row["token_expired"] = claims.get("token_expired")
                row["token_expires_at"] = claims.get("token_expires_at")
            session = result.get("session") or {}
            user = session.get("user") or {}
            account = session.get("account") or {}
            if user.get("id"):
                row["user_id"] = user.get("id")
            if user.get("name") is not None:
                row["user_name"] = user.get("name")
            if account.get("planType"):
                row["plan_type"] = account.get("planType")
            if session.get("expires"):
                row["expires_at"] = session.get("expires")
            row["live_check_proxy_used"] = result.get("proxy_used") or row.get("live_check_proxy_used")
            row["live_check_fingerprint_text"] = result.get("fingerprint_text") or row.get("live_check_fingerprint_text")
            if result.get("fingerprint"):
                row["live_check_fingerprint"] = result.get("fingerprint")
            row["live_check_error"] = None

        row["copy_line"] = _account_line(row)
        return True


def claim_account_totp_setup(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 2FA 设置任务；已有未超时任务时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("totp_setup_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "totp_setup_queued_at" if current_status == "queued" else "totp_setup_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["totp_setup_status"] = "queued"
        row["totp_setup_ok"] = False
        row["totp_setup_trigger"] = str(trigger or "manual")
        row["totp_setup_queued_at"] = now
        row["totp_setup_started_at"] = None
        row["totp_setup_completed_at"] = None
        row["totp_setup_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def mark_account_totp_setup_running(acc_id: int) -> bool:
    """把 2FA 设置任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("totp_setup_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["totp_setup_status"] = "running"
        row["totp_setup_started_at"] = now
        row["totp_setup_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_totp_secret(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 2FA/TOTP 设置结果。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["totp_setup_status"] = status
        row["totp_setup_ok"] = ok
        row["totp_setup_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["totp_setup_completed_at"] = _now()
        row["totp_setup_error"] = None if ok or status == "running" else result.get("error")
        secret = str(result.get("totp_secret") or "").strip()
        if ok and secret:
            row["totp_secret"] = secret
        if result.get("message") is not None:
            row["totp_setup_message"] = result.get("message")
        row["copy_line"] = _account_line(row)
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def claim_account_password_backfill(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号补密码任务；已有未超时任务时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("password_backfill_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "password_backfill_queued_at" if current_status == "queued" else "password_backfill_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["password_backfill_status"] = "queued"
        row["password_backfill_ok"] = False
        row["password_backfill_trigger"] = str(trigger or "manual")
        row["password_backfill_queued_at"] = now
        row["password_backfill_started_at"] = None
        row["password_backfill_completed_at"] = None
        row["password_backfill_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def mark_account_password_backfill_running(acc_id: int) -> bool:
    """把补密码任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("password_backfill_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["password_backfill_status"] = "running"
        row["password_backfill_started_at"] = now
        row["password_backfill_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_password_backfill(acc_id: int, result: dict | None = None) -> bool:
    """更新账号补密码结果；成功时把密码合并进 extra_json.registration_password。

    result 可选携带 access_token / token_expires_at（验证登录成功后顺带刷新）。
    """
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["password_backfill_status"] = status
        row["password_backfill_ok"] = ok
        row["password_backfill_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["password_backfill_completed_at"] = _now()
        row["password_backfill_error"] = None if ok or status == "running" else result.get("error")
        password = str(result.get("password") or "").strip()
        if ok and password:
            # extra_json 是注册时写入的完整素材，合并写回而不是整体替换；
            # 顶层 registration_password 同步一份，方便直接按字段搜索。
            extra_raw = row.get("extra_json")
            if isinstance(extra_raw, str) and extra_raw.strip():
                try:
                    extra = json.loads(extra_raw)
                except Exception:
                    extra = {}
            elif isinstance(extra_raw, dict):
                extra = extra_raw
            else:
                extra = {}
            if not isinstance(extra, dict):
                extra = {}
            extra["registration_password"] = password
            row["extra_json"] = json.dumps(extra, ensure_ascii=False)
            row["registration_password"] = password
        new_token = str(result.get("access_token") or "").strip()
        if ok and new_token:
            row["access_token"] = new_token
            if result.get("token_expires_at"):
                row["token_expires_at"] = result.get("token_expires_at")
        if result.get("message") is not None:
            row["password_backfill_message"] = result.get("message")
        row["copy_line"] = _account_line(row)
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def recover_interrupted_password_backfills() -> list[dict]:
    """服务启动时把中断的补密码任务标记为失败，返回被中断的账号列表。"""
    with _LOCK:
        rows = _load_accounts()
        interrupted: list[dict] = []
        now = _now()
        for row in rows:
            if row.get("password_backfill_status") not in {"queued", "running"}:
                continue
            row["password_backfill_status"] = "failed"
            row["password_backfill_ok"] = False
            row["password_backfill_error"] = "WebUI 重启导致补密码任务中断，请重新执行"
            row["password_backfill_completed_at"] = now
            row["updated_at"] = now
            interrupted.append({"id": row.get("id"), "email": row.get("email")})
        if interrupted:
            _save_accounts(rows)
        return interrupted


def claim_account_card_rebind(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号换绑卡任务；已有未超时任务时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("card_rebind_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "card_rebind_queued_at" if current_status == "queued" else "card_rebind_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["card_rebind_status"] = "queued"
        row["card_rebind_ok"] = False
        row["card_rebind_trigger"] = str(trigger or "manual")
        row["card_rebind_queued_at"] = now
        row["card_rebind_started_at"] = None
        row["card_rebind_completed_at"] = None
        row["card_rebind_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def mark_account_card_rebind_running(acc_id: int) -> bool:
    """把换绑卡任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("card_rebind_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["card_rebind_status"] = "running"
        row["card_rebind_started_at"] = now
        row["card_rebind_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_card_rebind(acc_id: int, result: dict | None = None) -> bool:
    """更新账号换绑卡结果；成功时 force 覆盖 pay_card_label。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["card_rebind_status"] = status
        row["card_rebind_ok"] = ok
        row["card_rebind_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["card_rebind_completed_at"] = _now()
        row["card_rebind_error"] = None if ok or status == "running" else result.get("error")
        label = str(result.get("card_label") or "").strip()
        if ok and label:
            row["card_rebind_card_label"] = label
        if result.get("message") is not None:
            row["card_rebind_message"] = result.get("message")
        row["copy_line"] = _account_line(row)
        row["updated_at"] = _now()
        _save_accounts(rows)
    if ok and label:
        # 换绑后扣款卡就是新卡，对账字段必须跟上（force 覆盖首次支付记录）。
        set_account_pay_card(int(acc_id), label, force=True)
    return True


def recover_interrupted_card_rebinds() -> list[dict]:
    """服务启动时把中断的换绑卡任务标记为失败，返回被中断的账号列表。"""
    with _LOCK:
        rows = _load_accounts()
        interrupted: list[dict] = []
        now = _now()
        for row in rows:
            if row.get("card_rebind_status") not in {"queued", "running"}:
                continue
            row["card_rebind_status"] = "failed"
            row["card_rebind_ok"] = False
            row["card_rebind_error"] = "WebUI 重启导致换绑卡任务中断，请重新执行"
            row["card_rebind_completed_at"] = now
            row["updated_at"] = now
            interrupted.append({"id": row.get("id"), "email": row.get("email")})
        if interrupted:
            _save_accounts(rows)
        return interrupted


def update_account_single_seat(acc_id: int, result: dict | None = None) -> bool:
    """记录单席位 Team 探测结果。

    supported=True 表示服务端允许这个账号只买 1 席，提链时能省一半钱。
    探测失败只记时间和原因，不写 supported，避免把"没测出来"当成"不支持"。
    """
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        row["single_seat_checked_at"] = result.get("checked_at") or _now()
        if result.get("ok"):
            row["single_seat_supported"] = bool(result.get("supported"))
            row["single_seat_minimum"] = result.get("minimum_seats")
            if result.get("free_trial") is not None:
                row["team_free_trial"] = bool(result.get("free_trial"))
            else:
                row.pop("team_free_trial", None)
            if result.get("checkout_total_minor") is not None:
                row["team_checkout_total_minor"] = int(result.get("checkout_total_minor"))
            else:
                row.pop("team_checkout_total_minor", None)
            if result.get("checkout_total_amount") is not None:
                row["team_checkout_total_amount"] = float(result.get("checkout_total_amount"))
            else:
                row.pop("team_checkout_total_amount", None)
            if result.get("checkout_subtotal_minor") is not None:
                row["team_checkout_subtotal_minor"] = int(result.get("checkout_subtotal_minor"))
            else:
                row.pop("team_checkout_subtotal_minor", None)
            if result.get("checkout_subtotal_amount") is not None:
                row["team_checkout_subtotal_amount"] = float(result.get("checkout_subtotal_amount"))
            else:
                row.pop("team_checkout_subtotal_amount", None)
            if result.get("checkout_discount_minor") is not None:
                row["team_checkout_discount_minor"] = int(result.get("checkout_discount_minor"))
            else:
                row.pop("team_checkout_discount_minor", None)
            if result.get("checkout_discount_amount") is not None:
                row["team_checkout_discount_amount"] = float(result.get("checkout_discount_amount"))
            else:
                row.pop("team_checkout_discount_amount", None)
            if result.get("checkout_discount_pct") is not None:
                row["team_checkout_discount_pct"] = float(result.get("checkout_discount_pct"))
            else:
                row.pop("team_checkout_discount_pct", None)
            disc_label = str(result.get("checkout_discount_label") or "").strip()[:120]
            if disc_label:
                row["team_checkout_discount_label"] = disc_label
            else:
                row.pop("team_checkout_discount_label", None)
            cur = str(result.get("checkout_currency") or result.get("currency") or "").strip().upper()
            if cur:
                row["team_checkout_currency"] = cur
            else:
                row.pop("team_checkout_currency", None)
            row["single_seat_error"] = None
        else:
            row["single_seat_error"] = str(result.get("error") or "")[:300]
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def recover_interrupted_totp_setups() -> list[dict]:
    """服务启动时恢复上次进程中断的 2FA 设置状态。

    返回被中断的账号列表，便于调用方重新入队：2FA 跑在后台线程里，一次
    部署重启就会把它打断（线上 #42 就是这么丢的），留个 failed 等人手动点
    很容易被忽略。
    """
    with _LOCK:
        rows = _load_accounts()
        interrupted: list[dict] = []
        now = _now()
        for row in rows:
            if row.get("totp_setup_status") not in {"queued", "running"}:
                continue
            row["totp_setup_status"] = "failed"
            row["totp_setup_ok"] = False
            row["totp_setup_error"] = "WebUI 重启导致 2FA 设置中断，已自动重新排队"
            row["totp_setup_completed_at"] = now
            row["updated_at"] = now
            interrupted.append({
                "id": int(row.get("id") or 0),
                "email": str(row.get("email") or ""),
                "access_token": str(row.get("access_token") or ""),
                "proxy_used": str(row.get("proxy_used") or ""),
                "trigger": str(row.get("totp_setup_trigger") or "registration_auto"),
            })
        if interrupted:
            _save_accounts(rows)
        return interrupted


def totp_setup_activity() -> dict:
    """2FA 队列的当前活动快照，供自动注册调度判断上一个号是否收尾。

    pending_since 取 queued/running 账号里最早的入队/开始时刻，让调度方能对
    卡死的 2FA 设置超时上限，而不是无限期等待。
    """
    _ensure_sqlite()
    status_expr = "lower(COALESCE(CAST(json_extract(payload, '$.totp_setup_status') AS TEXT), ''))"
    with closing(_sqlite_conn()) as conn:
        pending = int(conn.execute(
            f"SELECT COUNT(*) FROM accounts WHERE {status_expr} IN ('queued','running')"
        ).fetchone()[0])
        pending_since = conn.execute(
            f"""SELECT MIN(COALESCE(
                    CAST(json_extract(payload, '$.totp_setup_started_at') AS TEXT),
                    CAST(json_extract(payload, '$.totp_setup_queued_at') AS TEXT)))
                FROM accounts WHERE {status_expr} IN ('queued','running')"""
        ).fetchone()[0]
        last_completed = conn.execute(
            f"""SELECT MAX(CAST(json_extract(payload, '$.totp_setup_completed_at') AS TEXT))
                FROM accounts WHERE {status_expr} NOT IN ('queued','running')"""
        ).fetchone()[0]
    return {
        "pending": pending,
        "pending_since": str(pending_since) if pending_since else None,
        "last_completed_at": str(last_completed) if last_completed else None,
    }


def claim_account_live_check(acc_id: int, trigger: str = "manual", *, run_id: str | None = None, expected_email: str | None = None) -> bool:
    """原子占用账号查活任务；已有 queued/running 时返回 False。"""
    with _LOCK, _account_for_update(acc_id) as row:
        if row is None or (expected_email is not None
                           and str(row.get("email") or "").strip().lower() != expected_email.strip().lower()):
            return False
        if row.get("live_check_status") in {"queued", "running"}:
            try:
                stamp_key = "live_check_queued_at" if row.get("live_check_status") == "queued" else "live_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if row.get("live_check_status") == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["live_check_status"] = "queued"
        row["live_check_run_id"] = run_id
        row["live_check_ok"] = False
        row["live_check_trigger"] = str(trigger or "manual")
        row["live_check_queued_at"] = now
        row["live_check_started_at"] = None
        row["live_checked_at"] = None
        row["live_check_error"] = None
        row["updated_at"] = now
        return True


def recover_interrupted_live_checks() -> int:
    """服务启动时恢复上次进程中断的查活状态，避免 queued/running 卡死。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("live_check_status") not in {"queued", "running"}:
                continue
            row["live_check_status"] = "failed"
            row["live_check_ok"] = False
            row["live_check_error"] = "WebUI 重启或任务异常中断，请重新查活"
            row["live_checked_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def mark_account_live_check_running(acc_id: int, *, run_id: str | None = None) -> bool:
    """把账号查活任务标记为运行中。"""
    with _LOCK, _account_for_update(acc_id) as row:
        if row is None or row.get("live_check_status") not in {"queued", "running"}:
            return False
        if run_id is not None and row.get("live_check_run_id") != run_id:
            return False
        now = _now()
        row["live_check_status"] = "running"
        row["live_check_started_at"] = now
        row["live_check_error"] = None
        row["updated_at"] = now
        return True


def update_accounts_note(account_ids: list[int] | None, note: str) -> tuple[list[dict], list[dict]]:
    """
    批量更新已注册账号备注。
    返回 (updated, skipped)，updated/skipped 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        text = str(note or "")
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["note"] = text
            row["note_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "note": text, "note_updated_at": now})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def archive_account(acc_id: int, archived: bool = True) -> bool:
    """归档/取消归档单个已注册账号。归档不会删除 token，只影响默认账号列表查询。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["archived"] = bool(archived)
        row["archived_at"] = now if archived else None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def archive_accounts(account_ids: list[int] | None, archived: bool = True) -> tuple[list[dict], list[dict]]:
    """批量归档/取消归档账号。返回 (updated, skipped)。"""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["archived"] = bool(archived)
            row["archived_at"] = now if archived else None
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "archived": bool(archived), "archived_at": row.get("archived_at")})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def count_accounts() -> int:
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])


def delete_account(acc_id: int | None = None, email: str | None = None) -> bool:
    """从 SQLite 删除一个已注册账号记录，并清理关联的 Agent 凭证。"""
    with _LOCK:
        rows = _load_accounts()
        target_email = (email or "").lower()
        new_rows = []
        deleted_ids = []
        deleted = False
        for row in rows:
            match_id = acc_id is not None and int(row.get("id") or 0) == int(acc_id)
            match_email = bool(target_email) and (row.get("email") or "").lower() == target_email
            if match_id or match_email:
                deleted = True
                deleted_ids.append(int(row.get("id") or 0))
                continue
            new_rows.append(row)
        if not deleted:
            return False
        _save_accounts(new_rows)
        if deleted_ids:
            _ensure_sqlite()
            with closing(_sqlite_conn()) as conn:
                conn.executemany("DELETE FROM codex_agent_accounts WHERE account_id=?", [(x,) for x in deleted_ids])
                conn.commit()
        return True


def delete_deactivated_accounts(archived: str = "0") -> int:
    """按归档范围直接删除已废账号及其 Agent 凭证；不重写其他账号或巡检历史。"""
    if archived not in {"0", "only", "all"}:
        raise ValueError("archived 必须为 0、only 或 all")
    where = "lower(trim(COALESCE(json_extract(payload, '$.live_check_status'), ''))) = 'deactivated'"
    params = ()
    if archived != "all":
        where += " AND archived=?"
        params = (int(archived == "only"),)
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM codex_agent_accounts WHERE account_id IN "
                         "(SELECT id FROM accounts WHERE " + where + ")", params)
            return conn.execute("DELETE FROM accounts WHERE " + where, params).rowcount


def delete_accounts(account_ids: list[int] | None = None, emails: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """
    批量删除已注册账号。
    返回 (deleted, skipped)，deleted 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().isdigit()}
    email_set = {(e or "").lower() for e in (emails or []) if e}
    deleted: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        new_rows = []
        seen_ids: set[int] = set()
        seen_emails: set[str] = set()
        for row in rows:
            row_id = int(row.get("id") or 0)
            row_email = (row.get("email") or "").lower()
            if row_id in ids or row_email in email_set:
                deleted.append({"id": row_id, "email": row.get("email")})
                seen_ids.add(row_id)
                seen_emails.add(row_email)
                continue
            new_rows.append(row)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        for item in email_set - seen_emails:
            skipped.append({"email": item, "reason": "账号不存在"})
        if deleted:
            _save_accounts(new_rows)
            _ensure_sqlite()
            with closing(_sqlite_conn()) as conn:
                conn.executemany("DELETE FROM codex_agent_accounts WHERE account_id=?", [(x["id"],) for x in deleted])
                conn.commit()
    return deleted, skipped


# ============================================================
# outlook_pool
# ============================================================

def import_outlook_accounts(records: list[dict]) -> tuple[int, int]:
    """
    批量导入 Outlook 账号。
    records 元素：{email, password, client_id, refresh_token}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_outlook()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": (raw.get("password") or "").strip(),
                "client_id": (raw.get("client_id") or raw.get("clientId") or "").strip(),
                "refresh_token": (raw.get("refresh_token") or raw.get("refreshToken") or "").strip(),
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _outlook_line(row)
            rows.append(row)
            inserted += 1
        _save_outlook(rows)
        return inserted, skipped


def import_registered_email_accounts(records: list[dict], source: str | None) -> tuple[int, int]:
    """
    把邮箱素材直接导入为“已注册成功账号”，用于跳过注册、直接在账号页补跑 Codex 授权。

    source:
      - outlook: records 元素 {email,password,client_id,refresh_token[,access_token,totp_secret]}
      - generic_api: records 元素 {email,code_url[,access_token,totp_secret]}
      - imap: records 元素 {email,imap_password,imap_server,imap_port,imap_ssl}
      - akmoz: records 元素 {email,password,code_url[,access_token,totp_secret]}

    akmoz 与 generic_api 结构相近（都是靠取码链接拿 OTP），但它额外带邮箱密码，
    而密码是后续 reauth/查活要用的，所以要落进账号记录，不能像 generic_api
    那样丢掉。

    返回 (新增账号数, 跳过数)。已存在账号会跳过；邮箱池中已存在的素材会复用并标记 used。
    """
    source = (source or "").strip().lower()
    if source not in ("outlook", "generic_api", "imap", "akmoz"):
        raise ValueError("source 必须显式传入 outlook / generic_api / imap / akmoz")

    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        imap_rows = _load_imap_emails()
        akmoz_rows = _load_akmoz_emails()
        inserted = skipped = 0

        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(accounts, email):
                skipped += 1
                continue

            now = _now()
            original_line = email
            pool_row = None

            if source == "imap":
                password = str(raw.get("imap_password") or raw.get("password") or "").strip()
                server = str(raw.get("imap_server") or raw.get("server") or "").strip()
                try:
                    port = int(raw.get("imap_port") or raw.get("port") or 993)
                except (TypeError, ValueError):
                    port = 0
                if not password or not server or not (1 <= port <= 65535):
                    skipped += 1
                    continue
                ssl_raw = raw.get("imap_ssl", raw.get("use_ssl", True))
                use_ssl = ssl_raw if isinstance(ssl_raw, bool) else str(ssl_raw).strip().lower() not in {"0", "false", "no", "off"}
                pool_row = _find_by_email(imap_rows, email)
                values = {
                    "imap_password": password, "imap_server": server, "imap_port": port,
                    "imap_username": str(raw.get("imap_username") or raw.get("username") or "").strip(),
                    "imap_ssl": bool(use_ssl),
                }
                if pool_row is None:
                    pool_row = {"id": _next_id(imap_rows), "email": email, **values,
                                "status": "used", "used_at": now,
                                "note": "导入为已注册账号，用于 Codex 授权", "imported_at": now}
                    imap_rows.append(pool_row)
                else:
                    pool_row.update(values)
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _imap_email_line(pool_row)
                original_line = _imap_email_line(pool_row)
            elif source == "akmoz":
                # akmoz 素材：email----password----code_url（可带中文说明前缀）。
                # 与 generic_api 一样靠取码链接拿 OTP，但多带邮箱密码——
                # 密码要落进账号记录，后面 reauth/查活会用到。
                code_url = str(raw.get("code_url") or raw.get("url") or "").strip()
                password = str(raw.get("password") or "").strip()
                if not code_url:
                    skipped += 1
                    continue
                pool_row = _find_by_email(akmoz_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(akmoz_rows),
                        "email": email,
                        "password": password,
                        "code_url": code_url,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    akmoz_rows.append(pool_row)
                else:
                    pool_row["code_url"] = code_url or pool_row.get("code_url")
                    pool_row["password"] = password or pool_row.get("password")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _akmoz_email_line(pool_row)
                original_line = _akmoz_email_line(pool_row)
            elif source == "generic_api":
                code_url = _normalize_generic_api_code_url(raw.get("code_url") or raw.get("url"))
                if not code_url:
                    skipped += 1
                    continue
                pool_row = _find_by_email(generic_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(generic_rows),
                        "email": email,
                        "code_url": code_url,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    generic_rows.append(pool_row)
                else:
                    pool_row["code_url"] = code_url or pool_row.get("code_url")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _generic_api_email_line(pool_row)
                original_line = _generic_api_email_line(pool_row)
            else:
                password = (raw.get("password") or "").strip()
                client_id = (raw.get("client_id") or raw.get("clientId") or "").strip()
                refresh_token = (raw.get("refresh_token") or raw.get("refreshToken") or "").strip()
                if not (password and client_id and refresh_token):
                    skipped += 1
                    continue
                pool_row = _find_by_email(outlook_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(outlook_rows),
                        "email": email,
                        "password": password,
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    outlook_rows.append(pool_row)
                else:
                    pool_row["password"] = password or pool_row.get("password")
                    pool_row["client_id"] = client_id or pool_row.get("client_id")
                    pool_row["refresh_token"] = refresh_token or pool_row.get("refresh_token")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _outlook_line(pool_row)
                original_line = _outlook_line(pool_row)

            row_id = _next_account_id()
            access_token = (raw.get("access_token") or raw.get("token") or "").strip()
            totp_secret = (raw.get("totp_secret") or raw.get("totp") or "").strip() or None
            account = {
                "id": row_id,
                "email": email,
                "created_at": now,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "user_id": raw.get("user_id"),
                "user_name": raw.get("user_name") or "Imported Account",
                "plan_type": raw.get("plan_type"),
                "expires_at": raw.get("expires_at"),
                "device_id": raw.get("device_id"),
                "proxy_used": raw.get("proxy_used"),
                "email_source": source,
                "extra_json": json.dumps({"imported_registered": True}, ensure_ascii=False),
                "codex_status": raw.get("codex_status") or "",
                "codex_error": raw.get("codex_error"),
                "updated_at": now,
                "original_email_line": original_line,
            }
            if source == "outlook":
                account["password"] = pool_row.get("password")
                account["client_id"] = pool_row.get("client_id")
                account["refresh_token"] = pool_row.get("refresh_token")
            elif source == "akmoz":
                # 密码是后续 reauth / 查活的关键材料，必须随账号带上。
                account["password"] = pool_row.get("password")
            account["copy_line"] = _account_line(account)
            accounts.append(account)

            pool_row["registered_account_id"] = row_id
            pool_row["access_token"] = access_token
            if totp_secret:
                pool_row["totp_secret"] = totp_secret
            inserted += 1

        _save_outlook(outlook_rows)
        _save_generic_api_emails(generic_rows)
        _save_imap_emails(imap_rows)
        _save_akmoz_emails(akmoz_rows)
        _save_accounts(accounts)
        return inserted, skipped


def import_seat_accounts(records: list[dict], *, assign_proxy: bool = True) -> tuple[int, int, int]:
    """
    导入席位账号（已注册好的现成账号）：邮箱 + 登录密码 + 2FA 密钥。

    records 元素：{email, password, totp_secret, proxy_used, original_line}
    assign_proxy=True 时，没有显式代理的行从代理池（config.proxy.PROXY_POOL）
    随机抽一个写入 proxy_used，与注册流程 pick_proxy() 的取法一致。

    密码同时写入 extra_json.registration_password（账号页“密码”列、复制登录凭据、
    查活登录都从这里读取）。2FA 写入 totp_secret，账号页可直接看验证码。

    返回 (新增数, 跳过数, 分配代理数, 新增账号列表)。已存在同邮箱账号会跳过；
    新增账号列表元素为 {"id", "email", "proxy_used"}，供导入后自动预热使用。
    """
    with _LOCK:
        accounts = _load_accounts()
        inserted = skipped = proxied = 0
        inserted_rows: list[dict] = []

        for raw in records:
            email = (raw.get("email") or "").strip()
            password = str(raw.get("password") or "").strip()
            if not email or not password:
                skipped += 1
                continue
            if _find_by_email(accounts, email):
                skipped += 1
                continue

            now = _now()
            totp_secret = (raw.get("totp_secret") or "").strip()
            proxy_used = (raw.get("proxy_used") or "").strip()
            if not proxy_used and assign_proxy:
                try:
                    # 延迟导入，避免 core.db 与 config 包产生循环依赖。
                    from config.proxy import pick_proxy

                    proxy_used = str(pick_proxy() or "").strip()
                except Exception:
                    proxy_used = ""
            if proxy_used:
                proxied += 1

            row_id = _next_account_id()
            account = {
                "id": row_id,
                "email": email,
                "created_at": now,
                "access_token": "",
                "totp_secret": totp_secret or None,
                "user_id": None,
                "user_name": raw.get("user_name") or "Imported Account",
                "plan_type": None,
                "expires_at": None,
                "device_id": None,
                "proxy_used": proxy_used or None,
                "email_source": "seat",
                "password": password,
                "extra_json": json.dumps(
                    {"registration_password": password, "imported_seat": True},
                    ensure_ascii=False,
                ),
                "codex_status": "",
                "codex_error": None,
                "updated_at": now,
                "original_email_line": (raw.get("original_line") or "").strip()
                or "----".join(p for p in (email, password, totp_secret) if p),
            }
            account["copy_line"] = _account_line(account)
            accounts.append(account)
            inserted_rows.append({"id": row_id, "email": email, "proxy_used": proxy_used})
            inserted += 1

        if inserted:
            _save_accounts(accounts)
        return inserted, skipped, proxied, inserted_rows


def list_seat_accounts_pending_warmup() -> list[dict]:
    """导入后从未执行过查活的席位账号（重启后恢复预热用）。

    只挑 imported_seat 标记且 live_check_status 为空的行；查活已跑过的
    （含真实失败）不重复排队。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        cur = conn.execute(
            "SELECT id, email, json_extract(payload, '$.proxy_used') AS proxy_used "
            "FROM accounts "
            # imported_seat 标记在 extra_json 里，而 extra_json 是嵌套的 JSON 字符串
            # （历史行也可能直接是对象），SQLite json_extract 对两种形式都能再解析。
            "WHERE json_extract(json_extract(payload, '$.extra_json'), '$.imported_seat') = 1 "
            "AND (json_extract(payload, '$.live_check_status') IS NULL "
            "     OR json_extract(payload, '$.live_check_status') = '') "
            "ORDER BY id"
        )
        return [dict(row) for row in cur.fetchall()]


def claim_next_outlook() -> dict | None:
    """原子领取一个可用 Outlook 账号并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_outlook(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_outlook(rows)
        return _decorate_outlook(row)


def clear_outlook_failures(email: str) -> bool:
    """注册成功后清掉失败计数，别让偶发失败累积成停用。"""
    with _LOCK:
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None or not row.get("consecutive_failures"):
            return False
        row["consecutive_failures"] = 0
        _save_outlook(rows)
        return True


def release_outlook(email: str, status: str = "available", note: str | None = None) -> None:
    """把账号状态改回 available，或标记为 used/failed/disabled。"""
    with _LOCK:
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_outlook(rows)


#: 同一邮箱连续失败多少次后停用，不再放回池子。
#: 领取按 id 升序，所以一个反复失败的邮箱会被每一轮都选中，把整个自动注册
#: 卡死（2026-11-02/03 就有一个邮箱连卡 28 轮）。
_EMAIL_MAX_CONSECUTIVE_FAILURES = 3


def release_unconsumed_outlook(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的 Outlook 邮箱。

    连续失败达到上限就停用而不是放回：否则它会一直是 id 最小的 available，
    被永远优先领取，后面的邮箱一个都轮不到。
    """
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        try:
            fails = int(row.get("consecutive_failures") or 0) + 1
        except (TypeError, ValueError):
            fails = 1
        row["consecutive_failures"] = fails
        if fails >= _EMAIL_MAX_CONSECUTIVE_FAILURES:
            row["status"] = "disabled"
            row["used_at"] = row.get("used_at") or _now()
            row["note"] = (f"连续失败 {fails} 次已停用，不再自动领取。"
                           f"最后一次：{str(note or '')[:160]}")
        else:
            row["status"] = "available"
            row["used_at"] = None
            if note is not None:
                row["note"] = f"[失败 {fails}/{_EMAIL_MAX_CONSECUTIVE_FAILURES}] {note}"
        _save_outlook(rows)
        return True


def delete_email_pool(email: str, source: str = "all") -> bool:
    """按邮箱及来源从统一邮箱池删除记录。

    ``source=all`` 必须真正查询三个来源，而不能退化成 Outlook；旧版 WebUI
    在“全部邮箱池”中删除通用 API/域名邮箱时就是因此一直返回未找到。直接
    删除 SQLite 行也避免了逐条批量删除时反复加载并重写整个邮箱池。
    """
    target = str(email or "").strip()
    source = str(source or "all").strip().lower()
    if not target:
        return False
    if source not in {"all", "outlook", "generic_api", "imap", "akmoz",
                      "cloudflare_domain"}:
        raise ValueError(f"非法邮箱来源: {source}")

    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            if source == "all":
                cur = conn.execute(
                    "DELETE FROM email_pool WHERE email = ? COLLATE NOCASE",
                    (target,),
                )
            else:
                db_source = (
                    _EMAIL_SOURCES["domain"]
                    if source == "cloudflare_domain"
                    else _EMAIL_SOURCES[source]
                )
                cur = conn.execute(
                    "DELETE FROM email_pool "
                    "WHERE email = ? COLLATE NOCASE AND source = ?",
                    (target, db_source),
                )
            conn.commit()
            return cur.rowcount > 0


def delete_outlook(email: str) -> bool:
    """从邮箱池彻底删除一个邮箱（按 email 匹配）。返回是否删到。"""
    return delete_email_pool(email, source="outlook")


def list_outlook_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="outlook", status=status, limit=limit, offset=0
    )["items"]


def outlook_pool_summary() -> dict:
    with _LOCK:
        out = _pool_summary_sql("outlook")
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_outlook_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_outlook(), email)
        return _decorate_outlook(row) if row else None


# ============================================================
# generic_api email pool
# ============================================================

def import_generic_api_emails(records: list[dict]) -> tuple[int, int]:
    """
    批量导入通用 API 取码邮箱。
    records 元素：{email, code_url}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_generic_api_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = _normalize_generic_api_code_url(raw.get("code_url") or raw.get("url"))
            if not email or not code_url:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _generic_api_email_line(row)
            rows.append(row)
            inserted += 1
        _save_generic_api_emails(rows)
        return inserted, skipped


def claim_next_generic_api_email() -> dict | None:
    """原子领取一个可用通用 API 邮箱并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_generic_api_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_generic_api_emails(rows)
        return _decorate_generic_api_email(row)


def release_generic_api_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把通用 API 邮箱状态改回 available，或标记为 failed/used。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)


def release_unconsumed_generic_api_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的通用 API 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)
        return True


def delete_generic_api_email(email: str) -> bool:
    """从通用 API 邮箱池彻底删除一个邮箱。"""
    return delete_email_pool(email, source="generic_api")


def list_generic_api_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="generic_api", status=status, limit=limit, offset=0
    )["items"]


def generic_api_email_pool_summary() -> dict:
    with _LOCK:
        return _pool_summary_sql("generic_api")


def get_generic_api_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_generic_api_emails(), email)
        return _decorate_generic_api_email(row) if row else None


# ============================================================
# Generic IMAP email pool
# ============================================================

def import_imap_emails(records: list[dict]) -> tuple[int, int]:
    """导入 IMAP 邮箱。用户名留空时客户端使用邮箱地址登录。"""
    with _LOCK:
        rows = _load_imap_emails()
        inserted = skipped = 0
        for raw in records:
            email = str(raw.get("email") or "").strip()
            password = str(raw.get("imap_password") or raw.get("password") or "").strip()
            server = str(raw.get("imap_server") or raw.get("server") or "").strip()
            try:
                port = int(raw.get("imap_port") or raw.get("port") or 993)
            except (TypeError, ValueError):
                port = 0
            if not email or not password or not server or not (1 <= port <= 65535) or _find_by_email(rows, email):
                skipped += 1
                continue
            ssl_raw = raw.get("imap_ssl", raw.get("use_ssl", True))
            use_ssl = ssl_raw if isinstance(ssl_raw, bool) else str(ssl_raw).strip().lower() not in {"0", "false", "no", "off"}
            row = {
                "id": _next_id(rows), "email": email,
                "imap_password": password, "imap_server": server, "imap_port": port,
                "imap_username": str(raw.get("imap_username") or raw.get("username") or "").strip(),
                "imap_ssl": bool(use_ssl), "status": "available", "used_at": None,
                "note": None, "imported_at": _now(),
            }
            row["copy_line"] = _imap_email_line(row)
            rows.append(row)
            inserted += 1
        _save_imap_emails(rows)
        return inserted, skipped


def claim_next_imap_email() -> dict | None:
    with _LOCK:
        rows = sorted(_load_imap_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"], row["used_at"], row["note"] = "used", _now(), None
        _save_imap_emails(rows)
        return _decorate_imap_email(row)


def release_imap_email(email: str, status: str = "available", note: str | None = None) -> None:
    with _LOCK:
        rows = _load_imap_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_imap_emails(rows)


def release_unconsumed_imap_email(email: str, note: str | None = None) -> bool:
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_imap_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"], row["used_at"] = "available", None
        if note is not None:
            row["note"] = note
        _save_imap_emails(rows)
        return True


def delete_imap_email(email: str) -> bool:
    return delete_email_pool(email, source="imap")


def list_imap_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(source="imap", status=status, limit=limit, offset=0)["items"]


def imap_email_pool_summary() -> dict:
    with _LOCK:
        return _pool_summary_sql("imap")


def get_imap_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_imap_emails(), email)
        return _decorate_imap_email(row) if row else None


# ============================================================
# Codex 授权账号（SQLite codex_accounts 表）
# ============================================================

def _codex_filter_sql(
    archived: str | bool | None = "0",
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if archived in (True, "1", "true", "yes", "only"):
        where.append("archived=1")
    elif archived not in ("all", "include"):
        where.append("archived=0")
    if date_from:
        value = str(date_from)
        where.append("created_at >= ?")
        params.append(value + ("T00:00:00" if len(value) == 10 else ""))
    if date_to:
        value = str(date_to)
        where.append("created_at <= ?")
        params.append(value + ("T23:59:59.999999" if len(value) == 10 else ""))
    if q and str(q).strip():
        where.append("lower(payload) LIKE ?")
        params.append("%" + str(q).strip().lower() + "%")
    return where, params


def _codex_content_to_record(content: dict) -> dict:
    """把 SQLite 中的 Codex payload 转成列表展示对象。"""
    fname = content.get("_filename", "")
    without_prefix = fname[5:-5] if fname.startswith("codex-") and fname.endswith(".json") else fname
    email = content.get("email") or without_prefix
    plan = ""
    if "-" in without_prefix and without_prefix.rsplit("-", 1)[-1].lower() in ("free", "plus", "team", "pro", "enterprise"):
        plan = without_prefix.rsplit("-", 1)[-1].lower()
        if not content.get("email"):
            email = without_prefix.rsplit("-", 1)[0]
    return {
        "filename": fname, "path": f"sqlite://codex_accounts/{fname}", "email": email, "plan": plan,
        "account_id": content.get("account_id", ""), "type": content.get("type", "codex"),
        "last_refresh": content.get("last_refresh", ""), "expired": content.get("expired", ""),
        "access_token_preview": (content.get("access_token", "") or "")[:32],
        "size": content.get("_size", 0), "mtime": content.get("_mtime", ""),
        "exported_at": content.get("_exported_at"), "exported_count": content.get("_exported_count", 0),
        "archived": bool(content.get("_archived")), "archived_at": content.get("_archived_at"),
    }


def list_codex_accounts_page(
    archived: str | bool | None = "0",
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """直接在 codex_accounts 表执行分页查询，不读取 codex_accounts/ 文件。"""
    _ensure_sqlite()
    limit = max(1, int(limit))
    offset = max(0, int(offset or 0))
    where, params = _codex_filter_sql(archived, date_from, date_to, q)
    clause = " AND ".join(where) if where else "1=1"
    with closing(_sqlite_conn()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM codex_accounts WHERE {clause}", params).fetchone()[0])
        latest = str(conn.execute(
            f"SELECT COALESCE(MAX(updated_at), '') FROM codex_accounts WHERE {clause}", params
        ).fetchone()[0] or "")
        rows = conn.execute(
            f"SELECT payload FROM codex_accounts WHERE {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = [_codex_content_to_record(json.loads(row["payload"])) for row in rows]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "revision": f"{total}:{latest}",
    }


def list_codex_accounts(
    archived: str | bool | None = "0",
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
) -> list[dict]:
    """从 SQLite 读取 Codex 凭证元数据，不扫描 codex_accounts/ 文件。"""
    _ensure_sqlite()
    where, params = _codex_filter_sql(archived, date_from, date_to, q)
    clause = " AND ".join(where) if where else "1=1"
    with closing(_sqlite_conn()) as conn:
        rows = [json.loads(row["payload"]) for row in conn.execute(
            f"SELECT payload FROM codex_accounts WHERE {clause} ORDER BY created_at DESC, id DESC", params
        )]
    return [_codex_content_to_record(content) for content in rows]


def get_codex_credential_payload(email: str) -> dict | None:
    """按邮箱取 Codex 授权产物（access_token / refresh_token / id_token）。

    导入 sub2api 时要用它拼 oauth 凭据。同一邮箱可能有多条（不同 plan），
    取最近更新的那条。
    """
    mail = str(email or "").strip().lower()
    if not mail:
        return None
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        row = conn.execute(
            "SELECT payload FROM codex_accounts "
            "WHERE lower(email)=? AND archived=0 "
            "ORDER BY updated_at DESC LIMIT 1",
            (mail,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:  # noqa: BLE001
        return None


def upsert_codex_credential(content: dict, filename: str) -> str:
    """把 Codex 凭证写入 SQLite，返回逻辑文件名（不创建本地文件）。"""
    if not isinstance(content, dict) or not filename:
        raise ValueError("Codex 凭证或文件名无效")
    _ensure_sqlite()
    now = _now()
    with _LOCK, closing(_sqlite_conn()) as conn:
        old = conn.execute("SELECT payload, created_at FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
        meta = dict(content)
        if old:
            previous = json.loads(old["payload"])
            for key in ("_exported_at", "_exported_count", "_archived", "_archived_at"):
                if key not in meta:
                    meta[key] = previous.get(key)
            created_at = old["created_at"] or now
            account_id = conn.execute("SELECT id FROM codex_accounts WHERE filename=?", (filename,)).fetchone()[0]
        else:
            account_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM codex_accounts").fetchone()[0])
            created_at = now
        meta.update({"_filename": filename, "_size": len(json.dumps(content, ensure_ascii=False).encode("utf-8")), "_mtime": now})
        conn.execute(
            "INSERT INTO codex_accounts(id,filename,email,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(filename) DO UPDATE SET email=excluded.email, archived=excluded.archived, updated_at=excluded.updated_at, payload=excluded.payload",
            (account_id, filename, str(content.get("email") or ""), int(bool(meta.get("_archived"))), created_at, now, json.dumps(meta, ensure_ascii=False)),
        )
        conn.commit()
    return filename

def archive_codex(filename: str, archived: bool = True) -> dict | None:
    """归档/取消归档一条 Codex 授权凭证。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
            if not row:
                return None
            content = json.loads(row["payload"])
            rec = {"exported_at": content.get("_exported_at"), "exported_count": content.get("_exported_count", 0)}
        rec["archived"] = bool(archived)
        rec["archived_at"] = _now() if archived else None
        content.update({"_archived": rec["archived"], "_archived_at": rec["archived_at"]})
        with closing(_sqlite_conn()) as conn:
            conn.execute("UPDATE codex_accounts SET archived=?, updated_at=?, payload=? WHERE filename=?", (int(archived), _now(), json.dumps(content, ensure_ascii=False), filename))
            conn.commit()
        return rec


def read_codex_credential(filename: str) -> tuple[str, str]:
    """
    读取一个 codex-*.json 文件原始内容。
    Returns: (content_string, filename)
    抛 ValueError：文件名不合法（防目录穿越）/ 不存在。
    """
    with _LOCK:
        # 防注入：只允许 codex-*.json 模式，不允许路径分隔符
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
        if not row:
            raise ValueError(f"文件不存在: {filename}")
        content = json.loads(row["payload"])
        content = {k: v for k, v in content.items() if not k.startswith("_")}
        return json.dumps(content, ensure_ascii=False, indent=2), filename


def mark_codex_exported(filename: str) -> dict:
    """
    标记某个 codex 凭证已导出（导出计数 +1，记录最近导出时间）。
    Returns: 该 filename 当前的导出状态记录。
    """
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
            if not row:
                return {"exported_count": 0}
            content = json.loads(row["payload"])
        rec = {"exported_count": int(content.get("_exported_count", 0) or 0)}
        rec["exported_count"] = int(rec.get("exported_count", 0)) + 1
        rec["exported_at"] = _now()
        content.update({"_exported_count": rec["exported_count"], "_exported_at": rec["exported_at"]})
        with closing(_sqlite_conn()) as conn:
            conn.execute("UPDATE codex_accounts SET updated_at=?, payload=? WHERE filename=?", (_now(), json.dumps(content, ensure_ascii=False), filename))
            conn.commit()
        return rec


def reset_codex_exported(filename: str) -> None:
    """清掉某个 codex 凭证的导出状态（用户想重置时用）。"""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
            if not row:
                return
            content = json.loads(row["payload"])
            content.update({"_exported_count": 0, "_exported_at": None})
            conn.execute("UPDATE codex_accounts SET updated_at=?, payload=? WHERE filename=?", (_now(), json.dumps(content, ensure_ascii=False), filename))
            conn.commit()


def delete_codex_credential(filename: str) -> bool:
    """从 SQLite 删除一个 Codex 凭证。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            cur = conn.execute("DELETE FROM codex_accounts WHERE filename=?", (filename,))
            conn.commit()
            return cur.rowcount > 0


def codex_accounts_summary() -> dict:
    """codex 账号汇总：总数 / 已导出 / 未导出。"""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN COALESCE(CAST(json_extract(payload, '$._exported_count') AS INTEGER), 0) > 0 "
                "THEN 1 ELSE 0 END) AS exported "
                "FROM codex_accounts WHERE archived=0"
            ).fetchone()
        total = int(row["total"] or 0)
        exported = int(row["exported"] or 0)
        return {
            "total": total,
            "exported": exported,
            "pending": total - exported,
        }


# ============================================================
# registration_jobs
# ============================================================

def _new_job_row(
    rows: list[dict],
    *,
    job_id: int | None = None,
    email_source: str,
    job_type: str = "registration",
    parent_job_id: int | None = None,
    root_job_id: int | None = None,
    retry_attempt: int = 0,
    retry_action: str | None = None,
    email: str | None = None,
    account_id: int | None = None,
) -> dict:
    job_uuid = str(uuid.uuid4())
    log_file = str(_LOG_DIR / f"{job_uuid}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    return {
        "id": int(job_id) if job_id is not None else _next_id(rows),
        "job_uuid": job_uuid,
        "job_type": job_type,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "retry_attempt": int(retry_attempt or 0),
        "retry_action": retry_action,
        "email_source": email_source,
        "email": email,
        "status": "pending",
        "error_message": None,
        "log_file": log_file,
        "started_at": None,
        "completed_at": None,
        "account_id": account_id,
        "network_traffic": None,
        "created_at": _now(),
    }


def _reserve_job_id_floor(conn, value: int) -> None:
    conn.execute("INSERT INTO storage_meta(key,value) VALUES('registration_job_id_high_watermark',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER))",
                 (str(value),))


def _allocate_job_ids(conn, count: int) -> int:
    """调用方持有写事务；删除最大/全部任务后也不能让旧 Future 命中新任务。"""
    existing = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM registration_jobs").fetchone()[0])
    record = conn.execute("SELECT value FROM storage_meta WHERE key='registration_job_id_high_watermark'").fetchone()
    floor = max(existing, int(record[0]) if record else 0)
    _reserve_job_id_floor(conn, floor + count)
    return floor + 1


def _job_values(row: dict) -> tuple:
    return (int(row['id']), str(row.get('email') or ''), str(row.get('status') or ''),
            int(bool(row.get('archived'))), str(row.get('created_at') or ''),
            str(row.get('updated_at') or ''), json.dumps(row, ensure_ascii=False))


def _insert_job_rows(conn, rows: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO registration_jobs(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
        [_job_values(row) for row in rows],
    )


def create_jobs(count: int, email_source: str) -> list[dict]:
    """一次事务创建整批任务，不读取/重写历史任务 payload。"""
    count = int(count)
    if count <= 0:
        return []
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            first_id = _allocate_job_ids(conn, count)
            rows = [_new_job_row([], job_id=first_id + index, email_source=email_source)
                    for index in range(count)]
            now = datetime.now().isoformat(timespec="microseconds")
            for row in rows:
                row['updated_at'] = now
            _insert_job_rows(conn, rows)
    return rows


def create_job(email_source: str) -> dict:
    return create_jobs(1, email_source)[0]


def _finish_jobs_in_statuses(statuses: tuple[str, ...], status: str, error: str) -> int:
    """按状态索引批量转换，SQLite 写事务保证不会取消已领取的任务。"""
    with _LOCK:
        _ensure_sqlite()
        now = datetime.now().isoformat(timespec="microseconds")
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            marks = ','.join('?' for _ in statuses)
            result = conn.execute(
                "UPDATE registration_jobs SET status=?,updated_at=?,payload=json_set(payload,"
                "'$.status',?,'$.error_message',?,'$.completed_at',?,'$.updated_at',?) "
                f"WHERE status IN ({marks})", (status, now, status, error, now, now, *statuses))
            return result.rowcount


def recover_interrupted_registration_jobs() -> int:
    return _finish_jobs_in_statuses(('pending', 'running', 'stopping'), 'stopped',
                                   '服务重启，注册任务被中断并释放')


def cancel_pending_registration_jobs() -> int:
    return _finish_jobs_in_statuses(('pending',), 'cancelled', '用户手动取消')


def create_retry_job(
    source_job_id: int,
    *,
    job_type: str,
    email_source: str,
    email: str | None = None,
    account_id: int | None = None,
) -> tuple[dict, bool]:
    """原子创建重试子任务；同一任务链已有活跃任务时直接复用。"""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            record = conn.execute("SELECT payload FROM registration_jobs WHERE id=?", (int(source_job_id),)).fetchone()
            if record is None:
                raise LookupError("任务不存在")
            source = json.loads(record['payload'])
            if source.get('status') not in ('failed', 'stopped', 'cancelled'):
                raise ValueError(f"当前状态不支持重试：{source.get('status')}")
            root_id = int(source.get('root_job_id') or source['id'])
            active = conn.execute(
                "SELECT payload FROM registration_jobs WHERE CAST(json_extract(payload, '$.root_job_id') AS INTEGER)=? "
                "AND status IN ('pending','running','stopping') AND id!=? ORDER BY id LIMIT 1",
                (root_id, int(source_job_id)),
            ).fetchone()
            if active is not None:
                row = json.loads(active['payload'])
                if row.get('job_type', 'registration') != job_type:
                    raise ValueError(f"已有其他类型重试任务 #{row.get('id')} 在排队或运行中")
                return row, False
            successful = conn.execute(
                "SELECT id FROM registration_jobs WHERE CAST(json_extract(payload, '$.root_job_id') AS INTEGER)=? "
                "AND status='success' AND id!=? ORDER BY id DESC LIMIT 1", (root_id, int(source_job_id)),
            ).fetchone()
            if successful is not None:
                raise ValueError(f"后续重试任务 #{successful['id']} 已成功")
            attempt = conn.execute(
                "SELECT COALESCE(MAX(CAST(json_extract(payload, '$.retry_attempt') AS INTEGER)),0) "
                "FROM registration_jobs WHERE id=? OR CAST(json_extract(payload, '$.root_job_id') AS INTEGER)=?",
                (root_id, root_id),
            ).fetchone()[0]
            next_id = _allocate_job_ids(conn, 1)
            row = _new_job_row(
                [], job_id=next_id, email_source=email_source, job_type=job_type,
                parent_job_id=int(source_job_id), root_job_id=root_id,
                retry_attempt=int(attempt) + 1,
                retry_action='codex' if job_type == 'codex_retry' else 'registration',
                email=email, account_id=account_id,
            )
            row['updated_at'] = datetime.now().isoformat(timespec="microseconds")
            _insert_job_rows(conn, [row])
            return row, True


def update_job(
    job_id: int,
    *,
    status: str | None = None,
    email: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    account_id: int | None = None,
    network_traffic: dict | None = None,
    expected_statuses: tuple[str, ...] | None = None,
) -> bool:
    """索引定位原子读改写；可选状态 CAS，重复元数据更新不产生写入。"""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            record = conn.execute("SELECT payload FROM registration_jobs WHERE id=?", (int(job_id),)).fetchone()
            if record is None:
                return False
            row = json.loads(record['payload'])
            if expected_statuses is not None and row.get('status') not in expected_statuses:
                return False
            before = dict(row)
            if status is not None:
                row["status"] = status
            if email is not None:
                row["email"] = email
            if error is not None:
                row["error_message"] = error
            if started_at is not None:
                row["started_at"] = started_at
            if completed_at is not None:
                row["completed_at"] = completed_at
            if account_id is not None:
                row["account_id"] = account_id
            if network_traffic is not None:
                row["network_traffic"] = dict(network_traffic)
            if row == before:
                return True
            row['updated_at'] = datetime.now().isoformat(timespec="microseconds")
            values = _job_values(row)
            conn.execute(
                "UPDATE registration_jobs SET email=?,status=?,archived=?,created_at=?,updated_at=?,payload=? WHERE id=?",
                (*values[1:], values[0]),
            )
            return True


def list_jobs(limit: int = 100) -> list[dict]:
    with _LOCK:
        return [dict(r) for r in _query_collection("jobs", limit=limit)]


def list_jobs_page(limit: int = 50, offset: int = 0) -> dict:
    """索引分页；同一读事务内获取当前页、状态计数和 revision。"""
    _ensure_sqlite()
    limit, offset = max(1, int(limit)), max(0, int(offset or 0))
    with closing(_sqlite_conn()) as conn, conn:
        conn.execute("BEGIN")
        counts = {}
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM registration_jobs GROUP BY status"):
            status = str(row['status'] or 'unknown')
            counts[status] = counts.get(status, 0) + int(row['n'])
        total = sum(counts.values())
        counts['active'] = sum(counts.get(status, 0) for status in ('pending', 'running', 'stopping'))
        latest = conn.execute("SELECT COALESCE(MAX(updated_at), '') FROM registration_jobs").fetchone()[0]
        rows = [json.loads(row['payload']) for row in conn.execute(
            "SELECT payload FROM registration_jobs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))]
    return {'items': rows, 'total': total, 'offset': offset, 'limit': limit,
            'revision': f'{total}:{latest}', 'status_counts': counts}


def job_status_counts() -> dict:
    """在 SQLite 中聚合任务状态，避免为统计目的加载全部任务 payload。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        counts = {
            str(row["status"] or "unknown"): int(row["n"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM registration_jobs GROUP BY status"
            )
        }
    counts["active"] = sum(int(counts.get(status, 0) or 0) for status in ("pending", "running", "stopping"))
    return counts


def get_jobs_by_ids(job_ids: list[int]) -> list[dict]:
    """读取指定批次的最新任务快照，保持输入顺序。"""
    ids = list(dict.fromkeys(int(value) for value in job_ids))
    if not ids:
        return []
    _ensure_sqlite()
    found = {}
    with closing(_sqlite_conn()) as conn:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            for record in conn.execute(f"SELECT id,payload FROM registration_jobs WHERE id IN ({marks})", chunk):
                found[int(record['id'])] = json.loads(record['payload'])
    return [found[value] for value in ids if value in found]


def get_job(job_id: int) -> dict | None:
    rows = get_jobs_by_ids([job_id])
    return rows[0] if rows else None


def get_job_retry_context(jobs: list[dict]) -> dict[int, dict]:
    """批量获取当前页终态任务的后续成功重试和账号摘要，消除 N+1 全表读取。"""
    jobs = [job for job in jobs if job.get('status') in ('failed', 'stopped', 'cancelled')]
    if not jobs:
        return {}
    roots = list({int(job.get('root_job_id') or job['id']) for job in jobs})
    account_ids = set()
    for job in jobs:
        try:
            if job.get('account_id') is not None:
                account_ids.add(int(job['account_id']))
        except (TypeError, ValueError):
            pass
    emails = list({str(job.get('email') or '').strip().lower() for job in jobs} - {''})
    successes, accounts, by_email = {}, {}, {}
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn, conn:
        conn.execute('BEGIN')
        for start in range(0, len(roots), 400):
            chunk = roots[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            for row in conn.execute(
                f"SELECT id,CAST(json_extract(payload, '$.root_job_id') AS INTEGER) AS root_id FROM registration_jobs "
                f"WHERE CAST(json_extract(payload, '$.root_job_id') AS INTEGER) IN ({marks}) AND status='success' ORDER BY id DESC", chunk):
                successes.setdefault(int(row['root_id']), []).append({'id': int(row['id'])})
        for column, values in [('id', list(account_ids)), ('account_email_lower(email)', emails)]:
            for start in range(0, len(values), 400):
                chunk = values[start:start + 400]
                marks = ','.join('?' for _ in chunk)
                for row in conn.execute(
                    f"SELECT id,email,json_extract(payload, '$.codex_status') AS codex_status FROM accounts WHERE {column} IN ({marks}) ORDER BY id", chunk):
                    account = dict(row)
                    accounts[int(row['id'])] = account
                    email_key = str(row['email'] or '').lower()
                    previous = by_email.get(email_key)
                    if previous is None or int(row['id']) < int(previous['id']):
                        by_email[email_key] = account
    result = {}
    for job in jobs:
        root = int(job.get('root_job_id') or job['id'])
        success = next((row for row in successes.get(root, []) if row['id'] != int(job['id'])), None)
        try:
            account = accounts.get(int(job['account_id'])) if job.get('account_id') is not None else None
        except (TypeError, ValueError):
            account = None
        account = account or by_email.get(str(job.get('email') or '').strip().lower())
        result[int(job['id'])] = {'successful_retry': success, 'account': account}
    return result


def get_successful_retry_for_job(job_id: int) -> dict | None:
    """返回同一任务链中已成功的其他重试任务，用于保留原任务历史状态并阻止重复重试。"""
    job = get_job(job_id)
    if job is None:
        return None
    # 调用方只对失败终态任务检查成功重试；兼容其他状态的直接调用。
    context_job = dict(job, status='failed')
    context = get_job_retry_context([context_job]).get(int(job_id), {})
    match = context.get('successful_retry')
    return get_job(match['id']) if match else None


def delete_job(job_id: int, *, delete_log: bool = True, allow_running: bool = False) -> bool:
    """
    删除一个注册任务记录；默认同时删除该任务日志文件。返回是否删除到记录。
    默认不删除 running 任务，避免后台线程仍在执行但前端记录消失。
    """
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            record = conn.execute('SELECT payload FROM registration_jobs WHERE id=?', (int(job_id),)).fetchone()
            if record is None:
                return False
            row = json.loads(record['payload'])
            if not allow_running and row.get('status') in ('running', 'stopping'):
                return False
            _reserve_job_id_floor(conn, int(job_id))
            conn.execute('DELETE FROM registration_jobs WHERE id=?', (int(job_id),))

    if delete_log:
        log_file = row.get("log_file")
        if log_file:
            try:
                Path(log_file).unlink(missing_ok=True)
            except Exception:
                pass
    return True


# ============================================================
# 迁移与路径
# ============================================================

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _migrate_legacy_sqlite() -> dict:
    summary = {"sqlite_accounts_imported": 0, "sqlite_outlook_imported": 0, "sqlite_outlook_skipped": 0}
    if not _LEGACY_SQLITE.exists():
        return summary
    try:
        conn = sqlite3.connect(str(_LEGACY_SQLITE))
        conn.row_factory = sqlite3.Row
        if _table_exists(conn, "outlook_pool"):
            records = []
            statuses = []
            for row in conn.execute("SELECT * FROM outlook_pool").fetchall():
                records.append({
                    "email": row["email"],
                    "password": row["password"],
                    "client_id": row["client_id"],
                    "refresh_token": row["refresh_token"],
                })
                statuses.append({
                    "email": row["email"],
                    "status": row["status"],
                    "note": row["note"],
                })
            ins, skip = import_outlook_accounts(records)
            for item in statuses:
                if item["status"] != "available":
                    release_outlook(item["email"], status=item["status"], note=item["note"])
            summary["sqlite_outlook_imported"] += ins
            summary["sqlite_outlook_skipped"] += skip
        if _table_exists(conn, "registered_accounts"):
            for row in conn.execute("SELECT * FROM registered_accounts").fetchall():
                insert_account(
                    email=row["email"],
                    access_token=row["access_token"],
                    totp_secret=row["totp_secret"],
                    user_id=row["user_id"],
                    user_name=row["user_name"],
                    plan_type=row["plan_type"],
                    expires_at=row["expires_at"],
                    proxy_used=row["proxy_used"],
                    email_source=row["email_source"],
                    extra=json.loads(row["extra_json"]) if row["extra_json"] else None,
                )
                summary["sqlite_accounts_imported"] += 1
        conn.close()
    except Exception as exc:
        summary["sqlite_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def migrate_legacy_files() -> dict:
    """
    把历史 SQLite、accounts/*.json、旧邮箱 TXT/JSON 迁移到当前 SQLite 存储。
    多次调用是幂等的，不会生成或更新旧 JSON/TXT 文件。
    """
    summary = {
        "accounts_imported": 0,
        "outlook_imported": 0,
        "outlook_skipped": 0,
    }
    summary.update(_migrate_legacy_sqlite())

    accounts_dir = _PROJECT_ROOT / "accounts"
    if accounts_dir.exists():
        for jf in accounts_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if not data.get("email") or not data.get("access_token"):
                    continue
                extra = data.get("extra") or {}
                user = extra.get("user") or {}
                account = extra.get("account") or {}
                insert_account(
                    email=data["email"],
                    access_token=data["access_token"],
                    totp_secret=data.get("totp_secret"),
                    user_id=user.get("id"),
                    user_name=user.get("name"),
                    plan_type=account.get("planType"),
                    expires_at=extra.get("expires"),
                    extra=extra,
                )
                summary["accounts_imported"] += 1
            except Exception:
                continue

    for txt in (_PROJECT_ROOT / "outlook_accounts.txt", _OUTLOOK_TXT):
        if txt.exists():
            records = []
            for line in txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                # 支持 4 段或 6 段格式
                if len(parts) == 4:
                    email, password, client_id, refresh_token = (p.strip() for p in parts)
                elif len(parts) == 6:
                    email, password, client_id, refresh_token, _, _ = (p.strip() for p in parts)
                else:
                    continue
                records.append({
                    "email": email,
                    "password": password,
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                })
            ins, skip = import_outlook_accounts(records)
            summary["outlook_imported"] += ins
            summary["outlook_skipped"] += skip

    used = _PROJECT_ROOT / "outlook_accounts_used.json"
    if used.exists():
        try:
            emails = json.loads(used.read_text(encoding="utf-8"))
            for email in emails:
                release_outlook(email, status="used")
        except Exception:
            pass

    return summary


def db_path() -> Path:
    """返回 SQLite 主数据库路径（保留函数名兼容旧调用方）。"""
    _ensure_sqlite()
    return _active_sqlite_path()


def storage_paths() -> dict:
    return {
        "sqlite": str(_SQLITE_PATH),
        "logs_dir": str(_LOG_DIR),
    }


# ============================================================
# Domain email pool（Cloudflare 域名邮箱跟踪）
# ============================================================

_DOMAIN_EMAIL_JSON = _PROJECT_ROOT / "用于注册的域名邮箱.json"


def _load_domain_pool() -> list[dict]:
    return _load_collection("domain")


def _save_domain_pool(rows: list[dict]) -> None:
    _save_collection("domain", rows)


def _find_domain_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def claim_next_domain_email(email: str) -> dict:
    """记录一个新的域名邮箱地址到池中（标记为 available）。"""
    with _LOCK:
        rows = _load_domain_pool()
        if _find_domain_email(rows, email):
            # 已存在，直接返回
            row = _find_domain_email(rows, email)
            return row
        row = {
            "id": _next_id(rows),
            "email": email,
            "status": "available",
            "used_at": None,
            "note": None,
            "created_at": _now(),
        }
        rows.append(row)
        _save_domain_pool(rows)
        return dict(row)


def release_domain_email(email: str, status: str = "available", note: str | None = None) -> None:
    """更新域名邮箱状态。"""
    with _LOCK:
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)


def release_unconsumed_domain_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的域名邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)
        return True


def get_domain_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_domain_email(_load_domain_pool(), email)
        return dict(row) if row else None


def list_domain_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="cloudflare_domain", status=status, limit=limit, offset=0
    )["items"]


def domain_email_pool_summary() -> dict:
    with _LOCK:
        return _pool_summary_sql("domain")


def delete_domain_email(email: str) -> bool:
    """从域名邮箱池删除一个邮箱。"""
    return delete_email_pool(email, source="cloudflare_domain")


# 巡检任务与账号业务状态分开持久化，唯一索引保证跨线程/进程启动防重。
def _migrate_inspection_snapshots(conn):
    """升级旧任务：ID 快照只保存一次，统计 payload 不再携带整份清单。"""
    if conn.execute("SELECT 1 FROM storage_meta WHERE key='inspection_snapshots_v1'").fetchone():
        return
    for record in conn.execute("SELECT id,payload FROM account_inspections"):
        run = json.loads(record["payload"])
        if "account_ids" not in run:
            continue
        ids = run.pop("account_ids")
        conn.execute("INSERT OR IGNORE INTO account_inspection_snapshots VALUES (?,?)",
                     (record["id"], json.dumps(ids)))
        conn.execute("UPDATE account_inspections SET payload=? WHERE id=?",
                     (json.dumps(run, ensure_ascii=False), record["id"]))
    conn.execute("INSERT INTO storage_meta(key,value) VALUES ('inspection_snapshots_v1',?)", (_now(),))


def _inspection_conn():
    _ensure_sqlite()
    return _sqlite_conn()


def _inspection_payload(run):
    return json.dumps({k: v for k, v in run.items() if k != "account_ids"}, ensure_ascii=False)


def create_account_inspection(account_ids: list[int] | None = None, workers: int = 3, archived: str = "0") -> tuple[dict, bool]:
    if account_ids is not None:
        account_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids))
    with closing(_inspection_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute("SELECT payload FROM account_inspections WHERE status IN ('queued','running')").fetchone()
        if active:
            existing = json.loads(active[0])
            snapshot = conn.execute("SELECT account_ids FROM account_inspection_snapshots WHERE inspection_id=?",
                                    (existing["id"],)).fetchone()
            existing["account_ids"] = json.loads(snapshot[0]) if snapshot else []
            return existing, False
        if account_ids is None:
            # 与任务占用处于同一事务中，避免 OFFSET 分页遇到新增/归档/删除时漏账号。
            sql = "SELECT id FROM accounts"
            params = ()
            if archived != "all":
                sql += " WHERE archived=?"
                params = (int(archived == "only"),)
            account_ids = [row[0] for row in conn.execute(sql + " ORDER BY id DESC", params)]
        run = dict(status="queued", workers=workers, archived=archived, created_at=_now(), finished_at=None,
                   total_count=len(account_ids), checked_count=0, skipped_count=0, deactivated_skipped_count=0, invalid_count=0,
                   live_success_count=0, live_failed_count=0,
                   plan_success_count=0, plan_failed_count=0, error="",
                   failure_details_available=True, account_ids=account_ids)
        cursor = conn.execute("INSERT INTO account_inspections(status,payload) VALUES (?,?)", (run["status"], _inspection_payload(run)))
        run["id"] = cursor.lastrowid
        conn.execute("UPDATE account_inspections SET payload=? WHERE id=?", (_inspection_payload(run), run["id"]))
        conn.execute("INSERT INTO account_inspection_snapshots(inspection_id,account_ids) VALUES (?,?)",
                     (run["id"], json.dumps(account_ids)))
        return run, True


def save_account_inspection(run: dict, *, failure: dict | None = None) -> bool:
    # 终态不可覆盖，防止旧线程把已中断任务重新写为 running 或冲突新任务唯一索引。
    with closing(_inspection_conn()) as conn, conn:
        cursor = conn.execute("""UPDATE account_inspections SET status=?,payload=?
                                 WHERE id=? AND status IN ('queued', 'running')""",
                              (run["status"], _inspection_payload(run), run["id"]))
        saved = cursor.rowcount == 1
        if saved and failure is not None:
            # 统计和明细同事务保存；终态/旧 worker 不能补写历史或留下孤立明细。
            conn.execute("""INSERT INTO account_inspection_failures
                (inspection_id,account_id,email,status,error,failed_at) VALUES (?,?,?,?,?,?)
                ON CONFLICT(inspection_id,account_id) DO NOTHING""",
                (run["id"], int(failure["account_id"]), str(failure.get("email") or "")[:320],
                 str(failure.get("status") or "failed")[:40],
                 str(failure.get("error") or "查活失败，未返回具体原因")[:2000],
                 str(failure.get("failed_at") or _now())))
        return saved


def list_account_inspection_failures(inspection_id: int, limit: int = 20, offset: int = 0) -> dict | None:
    with closing(_inspection_conn()) as conn:
        conn.execute("BEGIN")
        task = conn.execute("SELECT payload FROM account_inspections WHERE id=?", (inspection_id,)).fetchone()
        if task is None:
            return None
        run = json.loads(task[0])
        items = conn.execute("""SELECT account_id,email,status,error,failed_at
            FROM account_inspection_failures WHERE inspection_id=? ORDER BY account_id LIMIT ? OFFSET ?""",
            (inspection_id, limit, offset)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM account_inspection_failures WHERE inspection_id=?",
                             (inspection_id,)).fetchone()[0]
    return dict(task={k: v for k, v in run.items() if k != "account_ids"},
                items=[dict(row) for row in items], total=total,
                details_available=bool(run.get("failure_details_available")), limit=limit, offset=offset)


def list_account_inspections(limit: int = 50, offset: int = 0) -> dict:
    with closing(_inspection_conn()) as conn:
        conn.execute("BEGIN")  # items、active、total 使用同一读快照。
        rows = conn.execute("SELECT payload FROM account_inspections ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        active = conn.execute("SELECT payload FROM account_inspections WHERE status IN ('queued','running')").fetchone()
        total = conn.execute("SELECT COUNT(*) FROM account_inspections").fetchone()[0]
    def public(payload):
        return {k: v for k, v in json.loads(payload).items() if k != "account_ids"}
    return dict(items=[public(r[0]) for r in rows], total=total,
                active=public(active[0]) if active else None)


def recover_interrupted_account_inspections(error: str = "WebUI 重启，巡检已中断；统计保留已完成部分") -> int:
    with closing(_inspection_conn()) as conn, conn:
        # 没有待恢复任务时是纯读操作，不能为每次空闲轮询竞争 SQLite 写锁。
        if not conn.execute("SELECT 1 FROM account_inspections WHERE status IN ('queued','running') LIMIT 1").fetchone():
            return 0
        conn.execute("BEGIN IMMEDIATE")
        # 加锁后重新读取，避免使用竞争写锁之前的旧快照。
        rows = conn.execute("SELECT payload FROM account_inspections WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            run = json.loads(row[0])
            run.update(status="interrupted", finished_at=_now(), error=error)
            conn.execute("UPDATE account_inspections SET status=?,payload=? WHERE id=?", (run["status"], _inspection_payload(run), run["id"]))
        return len(rows)


def update_account_team_checkout(acc_id: int, result: dict | None = None,
                                 *, clear: bool = False) -> bool:
    """更新账号 Team 结账长链接。

    clear=True 用于支付完成后清掉长链：链一旦付过就作废，留着只会让下次
    点支付时提示"可复用"，拿它再付一次必然失败。
    """
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if clear:
            for key in ("team_checkout_url", "team_checkout_created_at",
                        "team_checkout_error"):
                row.pop(key, None)
            row["updated_at"] = _now()
            _save_accounts(accounts)
            return True
        if result.get("url"):
            row["team_checkout_url"] = result.get("url")
            row["team_checkout_created_at"] = _now()
        if result.get("error"):
            row["team_checkout_error"] = result.get("error")
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True
