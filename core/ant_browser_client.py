# -*- coding: utf-8 -*-
"""Ant Browser Launch API 客户端（浏览器执行节点）。

对应 Ant Browser v1.9.x Launch API（源码对齐 backend/internal/launchcode）。
模式仿 core/roxybrowser_client.py：create/open/close/delete + 返回 CDP 入口。

要点：
- LaunchServer 只绑 127.0.0.1；远程（生产服务器）必须经 SSH 隧道访问。
- 统一 CDP 入口 = LaunchServer 端口本身（非 /api 路径反代到活动实例调试端口）；
  directDebugUrl = 实例独立调试端口，多实例并行时直连更稳。
- 一号一实例（ANT_ONE_PROFILE_PER_ACCOUNT=True）：每轮自动建实例，用完按配置清理。
- 高敏操作必须复用注册时的 profile（指纹延续）：调用方显式传 profile_id，
  传了就不再自动创建。
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests

from config import antbrowser as _cfg

logger = logging.getLogger(__name__)

# 进程内创建时隙：避免多 worker 同刻 POST /api/profiles
_CREATE_SLOT_LOCK = threading.Lock()
_NEXT_CREATE_SLOT = 0.0


def _wait_for_create_slot() -> None:
    global _NEXT_CREATE_SLOT

    interval = max(0.0, float(getattr(_cfg, "ANT_CREATE_INTERVAL", 1.0) or 0.0))
    if interval <= 0:
        return
    with _CREATE_SLOT_LOCK:
        now = time.monotonic()
        wait_for = max(0.0, _NEXT_CREATE_SLOT - now)
        _NEXT_CREATE_SLOT = max(now, _NEXT_CREATE_SLOT) + interval
    if wait_for > 0:
        logger.info("[Ant] /api/profiles 请求错峰，等待 %.2fs", wait_for)
        time.sleep(wait_for)


@dataclass
class AntOpenResult:
    profile_id: str
    raw: dict = field(default_factory=dict)
    cdp_url: str | None = None      # 统一入口（LaunchServer 端口，反代到活动实例）
    direct_debug_url: str | None = None  # 实例独立调试端口
    debug_port: int = 0
    launch_code: str = ""
    pid: int = 0
    created_by_run: bool = False


def _mask_proxy(proxy_url: str) -> str:
    parsed = urlparse(str(proxy_url or "").strip())
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://***:***@{host}{port}"
    return str(proxy_url or "").strip()


def _extra_launch_args() -> list[str]:
    raw = str(getattr(_cfg, "ANT_EXTRA_LAUNCH_ARGS", "") or "").strip()
    if not raw:
        return []
    out = []
    for part in raw.replace("\n", ",").split(","):
        item = part.strip()
        if item:
            out.append(item)
    return out


def _random_profile_name() -> str:
    prefix = str(getattr(_cfg, "ANT_PROFILE_NAME_PREFIX", "ant") or "ant").strip() or "ant"
    return f"{prefix}-{int(time.time() * 1000)}-{random.randrange(0x10000):04x}"


def _normalize_profile_id(value: str | None) -> str:
    text = str(value or "").strip()
    if text in ("-", "—", "无", "空", "none", "None", "null", "NULL"):
        return ""
    return text


def rewrite_cdp_url(url: str | None) -> str | None:
    """按 ANT_CDP_REWRITE_HOST 重写 CDP 地址的 host 部分。

    Ant 返回 http://127.0.0.1:<port>；跨机驱动（服务器经隧道接管）时端口一致、
    host 就是驱动侧的 127.0.0.1，因此默认无需重写；留出重写钩子以支持
    端口段转发/别名主机等场景。
    """
    if not url:
        return url
    host = str(getattr(_cfg, "ANT_CDP_REWRITE_HOST", "") or "").strip()
    if not host:
        return url
    try:
        parsed = urlparse(url if "//" in url else f"http://{url}")
        port = parsed.port or 0
        if ":" in host:
            host_part, port_part = host.rsplit(":", 1)
            port = int(port_part) if port_part.isdigit() else port
        else:
            host_part = host
        return f"http://{host_part}:{port}"
    except Exception:
        return url


class AntBrowserClient:
    def __init__(self, api_base: str | None = None, api_key: str | None = None):
        self.api_base = (api_base or _cfg.ANT_API_BASE).strip().rstrip("/")
        self.api_key = (api_key if api_key is not None else _cfg.ANT_API_KEY).strip()
        self.http = requests.Session()
        self.http.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if self.api_key:
            header = str(getattr(_cfg, "ANT_API_KEY_HEADER", "X-Ant-Api-Key") or "X-Ant-Api-Key")
            self.http.headers[header] = self.api_key
        # 本 client 实例创建过的 profile id：显式传入这些 id 时仍视为本轮创建，
        # 以便 cleanup 生命周期与 create->open 两步调用兼容。
        self._created_ids: set[str] = set()

    # ---------- 基础请求 ----------

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "timeout" in text
            or "timed out" in text
            or "connection" in text
            or "temporarily" in text
            or "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "http 429" in text
        )

    def request(self, method: str, path: str, *, json_body: dict | None = None, timeout: int | None = None) -> dict:
        url = urljoin(self.api_base + "/", path.lstrip("/"))
        method_u = method.upper()
        max_attempts = max(1, int(getattr(_cfg, "ANT_API_RETRIES", 3) or 3))
        base_delay = max(0.5, float(getattr(_cfg, "ANT_API_RETRY_DELAY", 2) or 2))
        timeout = timeout or max(5, int(getattr(_cfg, "ANT_API_TIMEOUT", 90) or 90))
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug("[Ant] %s %s body=%s attempt=%s/%s", method_u, url, json_body, attempt, max_attempts)
                resp = self.http.request(method_u, url, json=json_body, timeout=timeout)
                text = resp.text or ""
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"raw": text}
                if not (200 <= resp.status_code < 300):
                    # 409 = 实例已在运行等幂等场景，交给上层判断，不按错误抛
                    raise RuntimeError(f"Ant API 请求失败 {method_u} {path} HTTP {resp.status_code}: {text[:500]}")
                if isinstance(payload, dict) and payload.get("ok") is False:
                    msg = payload.get("error") or json.dumps(payload, ensure_ascii=False)[:500]
                    raise RuntimeError(f"Ant API 返回失败 {method_u} {path}: {msg}")
                return payload if isinstance(payload, dict) else {"data": payload}
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts or not self._is_retryable_error(exc):
                    raise
                delay = base_delay * attempt
                logger.warning(
                    "[Ant] API 请求失败，%.1fs 后重试：%s %s attempt=%s/%s error=%s",
                    delay, method_u, path, attempt, max_attempts, exc,
                )
                time.sleep(delay)
        raise last_exc or RuntimeError(f"Ant API 请求失败 {method_u} {path}")

    def try_request(self, method: str, path: str, *, json_body: dict | None = None, timeout: int | None = None) -> tuple[bool, dict | str]:
        try:
            return True, self.request(method, path, json_body=json_body, timeout=timeout)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def health(self) -> bool:
        ok, payload = self.try_request("GET", "/api/health", timeout=10)
        return bool(ok and isinstance(payload, dict) and payload.get("ok") is True)

    # ---------- 实例 CRUD ----------

    def list_profiles(self) -> list[dict]:
        payload = self.request("GET", "/api/profiles")
        items = payload.get("items") if isinstance(payload, dict) else None
        return [it for it in (items or []) if isinstance(it, dict)]

    def get_profile(self, profile_id: str) -> dict | None:
        ok, payload = self.try_request("GET", f"/api/profiles/{profile_id}")
        if ok and isinstance(payload, dict):
            return payload.get("profile") or payload
        return None

    def create_profile(self, *, name: str | None = None, proxy_url: str | None = None,
                       tags: list[str] | None = None, launch_args: list[str] | None = None) -> str:
        # 实例命名约定（2026-11-20 用户定）：账号实例直接用邮箱名，
        # 便于在 Ant UI 里对号入座；无邮箱场景（探针/测试）回退随机前缀名。
        clean = str(name or "").strip()
        display_name = clean if clean else _random_profile_name()
        body_profile: dict = {
            "profileName": display_name,
            "coreId": str(getattr(_cfg, "ANT_CORE_ID", "") or "").strip(),
            "proxyMode": "fixed",
            "proxyId": "__direct__",
            "proxyConfig": "direct://",
        }
        if tags:
            body_profile["tags"] = list(tags)
        extra_args = _extra_launch_args()
        if extra_args or launch_args:
            body_profile["launchArgs"] = list(dict.fromkeys([*(launch_args or []), *extra_args]))
        proxy_url = (proxy_url if proxy_url is not None else str(getattr(_cfg, "ANT_PROFILE_PROXY_URL", "") or "")).strip()
        if proxy_url and proxy_url != "direct://":
            parsed = urlparse(proxy_url)
            scheme = (parsed.scheme or "").lower()
            if scheme not in ("http", "https", "socks5", "socks5h", "socks4"):
                raise ValueError(f"Ant 暂不支持该代理协议: {scheme or '-'}")
            if not parsed.hostname:
                raise ValueError(f"代理格式缺少 host: {_mask_proxy(proxy_url)}")
            body_profile["proxyId"] = ""
            body_profile["proxyConfig"] = proxy_url
        _wait_for_create_slot()
        result = self.request("POST", "/api/profiles", json_body={"profile": body_profile, "autoLaunch": False})
        profile_id = str(result.get("profileId") or (result.get("profile") or {}).get("profileId") or "").strip()
        if not profile_id:
            raise RuntimeError(f"Ant 创建实例成功但未返回 profileId: {result}")
        self._created_ids.add(profile_id)
        return profile_id

    def update_profile(self, profile_id: str, profile_input: dict) -> dict:
        return self.request("PUT", f"/api/profiles/{profile_id}", json_body={"profile": profile_input, "autoLaunch": False})

    def delete_profile(self, profile_id: str) -> None:
        if not _normalize_profile_id(profile_id):
            return
        # 运行中的实例不能删除（409）：先停止并等状态收敛，再删除
        self.stop_profile(profile_id, raise_on_error=False)
        self._wait_profile_stopped(profile_id, timeout=15.0)
        try:
            self.request("DELETE", f"/api/profiles/{profile_id}")
            logger.info("[Ant] 已删除实例：%s", profile_id)
        except Exception as exc:
            logger.warning("[Ant] 删除实例失败：%s", exc)

    def _wait_profile_stopped(self, profile_id: str, *, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.status_profile(profile_id)
                if not status.get("running") and not status.get("debugReady"):
                    return True
            except Exception:
                return True  # 状态查询失败按已停处理，交给删除重试
            time.sleep(0.5)
        return False

    # ---------- 启动/停止 ----------

    def open_profile(self, profile_id: str | None = None, *,
                     start_urls: list[str] | None = None,
                     launch_args: list[str] | None = None,
                     proxy_url: str | None = None,
                     email: str | None = None) -> AntOpenResult:
        """启动实例并等待 CDP 就绪（POST /api/runtime/session）。

        profile_id 语义：
        - 显式传入（账号注册时绑定的实例 ID）= 高敏操作正确姿势，直接启动；
        - 不传：一号一实例开启时自动创建新实例；关闭时回退全局 ANT_PROFILE_ID。
        email：自动创建时的实例名（约定=账号邮箱）；不影响已有实例的启动。
        """
        explicit = _normalize_profile_id(profile_id)
        created_by_run = False
        if explicit:
            pid = explicit
            if pid in self._created_ids:
                created_by_run = True
        else:
            configured = _normalize_profile_id(getattr(_cfg, "ANT_PROFILE_ID", ""))
            one_profile = bool(getattr(_cfg, "ANT_ONE_PROFILE_PER_ACCOUNT", True))
            if one_profile or not configured:
                pid = self.create_profile(name=str(email or "").strip() or None,
                                          proxy_url=proxy_url)
                created_by_run = True
                logger.info("[Ant] 已创建实例：%s", pid)
            else:
                pid = configured

        body: dict = {"profileId": pid, "timeoutMs": int(getattr(_cfg, "ANT_RUNTIME_SESSION_TIMEOUT_MS", 60000) or 60000)}
        extra_args = _extra_launch_args()
        if extra_args or launch_args:
            body["launchArgs"] = list(dict.fromkeys([*(launch_args or []), *extra_args]))
        if start_urls:
            body["startUrls"] = list(start_urls)
        if proxy_url:
            body["proxyConfig"] = proxy_url
        logger.info(
            "[Ant] 启动实例：profile=%s created=%s timeoutMs=%s startUrls=%s",
            pid, created_by_run, body["timeoutMs"], start_urls or "-",
        )
        result = self.request("POST", "/api/runtime/session", json_body=body, timeout=max(60, body["timeoutMs"] // 1000 + 30))
        return self._build_open_result(pid, result, created_by_run)

    def _build_open_result(self, profile_id: str, payload: dict, created_by_run: bool) -> AntOpenResult:
        debug_port = int(payload.get("debugPort") or 0)
        cdp_url = str(payload.get("cdpUrl") or "").strip() or None
        direct = str(payload.get("directDebugUrl") or "").strip() or None
        if not cdp_url and direct:
            cdp_url = direct
        if debug_port and not direct:
            direct = f"http://127.0.0.1:{debug_port}"
        if not cdp_url and not direct:
            raise RuntimeError(f"Ant 已启动实例但未返回 CDP 地址: {payload}")
        # 跨机驱动（配置了 CDP host 重写）时，隧道通常只转发 Launch API 端口，
        # directDebugUrl 的动态调试端口不可达：此时统一入口 cdpUrl 是唯一可用路径，
        # 不返回 direct，避免调用方误连。
        remote_mode = bool(str(getattr(_cfg, "ANT_CDP_REWRITE_HOST", "") or "").strip())
        return AntOpenResult(
            profile_id=profile_id,
            raw=payload,
            cdp_url=rewrite_cdp_url(cdp_url),
            direct_debug_url=None if remote_mode else rewrite_cdp_url(direct),
            debug_port=debug_port,
            launch_code=str(payload.get("launchCode") or ""),
            pid=int(payload.get("pid") or 0),
            created_by_run=created_by_run,
        )

    def status_profile(self, profile_id: str) -> dict:
        return self.request("GET", f"/api/profiles/{profile_id}/status")

    def stop_profile(self, profile_id: str, *, raise_on_error: bool = True) -> None:
        if not _normalize_profile_id(profile_id):
            return
        try:
            self.request("POST", f"/api/profiles/{profile_id}/stop", json_body={})
            logger.info("[Ant] 已停止实例：%s", profile_id)
        except Exception as exc:
            if raise_on_error:
                raise
            logger.warning("[Ant] 停止实例失败：%s", exc)

    def active_profile(self) -> dict:
        return self.request("GET", "/api/runtime/active")

    def cleanup_profile(self, opened: AntOpenResult | None) -> None:
        """任务结束清理：关窗；本轮创建的实例按配置删除。

        一号一实例 + 高敏操作场景下，账号绑定 profile 的延续由调用方管理：
        只有 created_by_run=True 的实例才会被删除。
        """
        if not opened or not opened.profile_id:
            return
        keep_open = bool(getattr(_cfg, "ANT_KEEP_BROWSER_OPEN", False))
        if keep_open:
            logger.info("[Ant] ANT_KEEP_BROWSER_OPEN=True，保留实例现场：%s", opened.profile_id)
            return
        self.stop_profile(opened.profile_id, raise_on_error=False)
        if (
            bool(getattr(_cfg, "ANT_DELETE_PROFILE_AFTER_RUN", True))
            and opened.created_by_run
        ):
            self.delete_profile(opened.profile_id)
