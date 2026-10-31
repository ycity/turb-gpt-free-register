# -*- coding: utf-8 -*-
"""按代理出口复用 CloakBrowser 资料目录。

每次注册都开全新浏览器时，Cloudflare 的放行 cookie（cf_clearance）无法保留，
于是每个任务都要重新过一次人机验证，失败率明显偏高。这里给每个代理固定一个资料目录：

    - cf_clearance 绑定出口 IP 和指纹，所以按代理分目录，不同代理互不污染；
    - 目录由 CloakBrowser 自己固定指纹 seed（.cloakbrowser-seed），和出口 IP 保持一致；
    - 目录带独占锁，并发任务抢不到锁时回退到临时目录，不会因为 Chromium 的
      SingletonLock 直接启动失败；
    - 启动后只保留 Cloudflare 的 cookie，其余一律清空，绝不把上一个账号的登录态
      带进下一次注册。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ROOT = _PROJECT_ROOT / "cache" / "cloak-profiles"
_LOCK_NAME = ".turb-lock"
# 同进程内的目录锁；跨进程再靠锁文件。
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
# 只有这些 cookie 会被保留；其余（含 OpenAI 登录态）每次启动都清空。
CLOUDFLARE_COOKIE_NAMES = frozenset({
    "cf_clearance", "__cf_bm", "__cfseq", "__cfruid",
    "cf_chl_rc_i", "cf_chl_rc_ni", "cf_chl_rc_m",
})
# 锁文件超过这个时间视为上次进程崩溃残留。
_STALE_LOCK_SECONDS = 1800


@dataclass
class ProfileLease:
    """一次性的资料目录租约；用完必须 release()。"""

    path: Path
    key: str
    _local_lock: threading.Lock | None = None
    _lock_file: Path | None = None
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._lock_file is not None:
            try:
                self._lock_file.unlink(missing_ok=True)
            except OSError:
                logger.debug("[Cloak资料] 删除锁文件失败：%s", self._lock_file, exc_info=True)
        if self._local_lock is not None and self._local_lock.locked():
            try:
                self._local_lock.release()
            except RuntimeError:
                pass


def profile_root() -> Path:
    from config import cloakbrowser as cfg

    raw = str(getattr(cfg, "CLOAK_PROFILE_ROOT", "") or "").strip()
    root = Path(raw).expanduser() if raw else _DEFAULT_ROOT
    return root if root.is_absolute() else (_PROJECT_ROOT / root)


def profile_key(proxy_target: str) -> str:
    """按代理出口生成目录名；直连也有固定的一个目录。"""
    text = str(proxy_target or "").strip()
    if not text:
        return "direct"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _local_lock_for(key: str) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _take_lock_file(path: Path) -> Path | None:
    """独占创建锁文件；已存在且未过期则返回 None。"""
    lock_file = path / _LOCK_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - lock_file.stat().st_mtime
        except OSError:
            return None
        if age < _STALE_LOCK_SECONDS:
            return None
        # 上次进程异常退出留下的锁，接管它。
        logger.warning("[Cloak资料] 发现 %.0f 分钟前的残留锁，已接管：%s", age / 60, lock_file)
        try:
            lock_file.unlink(missing_ok=True)
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return None
    except OSError as exc:
        logger.warning("[Cloak资料] 创建目录或锁文件失败，本次改用临时资料目录：%s: %s", type(exc).__name__, exc)
        return None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass
    finally:
        os.close(fd)
    return lock_file


def acquire(proxy_target: str) -> ProfileLease | None:
    """取得该代理对应的资料目录；被占用或不可用时返回 None（调用方回退临时目录）。"""
    from config import cloakbrowser as cfg

    if not bool(getattr(cfg, "CLOAK_PERSIST_PROFILE", False)):
        return None
    key = profile_key(proxy_target)
    local_lock = _local_lock_for(key)
    if not local_lock.acquire(blocking=False):
        logger.info("[Cloak资料] 资料目录正被同进程的其他任务使用，本次改用临时目录：key=%s", key)
        return None
    path = profile_root() / key
    lock_file = _take_lock_file(path)
    if lock_file is None:
        local_lock.release()
        logger.info("[Cloak资料] 资料目录被占用或不可写，本次改用临时目录：key=%s", key)
        return None
    logger.info("[Cloak资料] 复用资料目录：key=%s path=%s 已存在=%s", key, path, (path / "Default").exists())
    return ProfileLease(path=path, key=key, _local_lock=local_lock, _lock_file=lock_file)


def keep_only_cloudflare_cookies(context) -> dict:
    """只留下 Cloudflare 放行 cookie，其余全部清空。

    返回 {kept, dropped}；读写 cookie 失败时不抛异常，但会把 kept 记为 -1，
    由调用方决定是否继续（上层会继续注册，最坏情况等于没有复用）。
    """
    try:
        cookies = list(context.cookies() or [])
    except Exception as exc:
        logger.warning("[Cloak资料] 读取 cookie 失败，跳过清理：%s: %s", type(exc).__name__, exc)
        return {"kept": -1, "dropped": 0}
    keep = [c for c in cookies if str(c.get("name") or "") in CLOUDFLARE_COOKIE_NAMES]
    try:
        context.clear_cookies()
        if keep:
            context.add_cookies(keep)
    except Exception as exc:
        # 清不干净就必须让上层知道：带着旧登录态注册会直接失败。
        logger.warning("[Cloak资料] 清理 cookie 失败：%s: %s", type(exc).__name__, exc)
        return {"kept": -1, "dropped": 0}
    dropped = len(cookies) - len(keep)
    logger.info("[Cloak资料] 已保留 %s 个 Cloudflare cookie，清除 %s 个其他 cookie", len(keep), dropped)
    return {"kept": len(keep), "dropped": dropped}


def cleanup_idle_profiles() -> int:
    """删除长期未使用的资料目录，返回删除个数。"""
    from config import cloakbrowser as cfg

    days = int(getattr(cfg, "CLOAK_PROFILE_MAX_IDLE_DAYS", 0) or 0)
    root = profile_root()
    if days <= 0 or not root.is_dir():
        return 0
    deadline = time.time() - days * 86400
    removed = 0
    for child in root.iterdir():
        if not child.is_dir() or (child / _LOCK_NAME).exists():
            continue
        try:
            if child.stat().st_mtime >= deadline:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        except OSError:
            logger.debug("[Cloak资料] 清理闲置目录失败：%s", child, exc_info=True)
    if removed:
        logger.info("[Cloak资料] 已清理 %s 个闲置超过 %s 天的资料目录", removed, days)
    return removed
