# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）

代理池每行格式：``[# ]代理URL[#名称]``
    - 行尾 ``#名称`` 是代理的备注名，例如 socks5h://user:pass@1.2.3.4:1080#美国家宽1
    - 行首 ``#`` 表示停用：保留在列表里，但不参与随机抽取
"""
import random
from urllib.parse import quote, urlparse

from config.env_loader import apply_env_overrides


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# 代理池使用的本地上游代理。填写后形成：本地上游 -> 代理池目标代理 -> ChatGPT；
# 留空则直接使用代理池中的目标代理，不启动链式中继。
PROXY_POOL_UPSTREAM_PROXY = ""

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；没有代理时才按 direct 运行
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = []

# 套餐查询 / Codex Agent Token 生成的上游代理。填写后形成：本地代理 -> 动态代理 -> ChatGPT。
# 例如 http://127.0.0.1:7897；留空则直接连接 PLAN_CHECK_PROXY。
PLAN_CHECK_UPSTREAM_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 3
PLAN_CHECK_RETRY_DELAY = 2.0

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 1.0
PLAN_CHECK_JITTER = 0.8


def _valid_port(value: str) -> bool:
    return value.isdigit() and 1 <= int(value) <= 65535


def normalize_proxy_url(value: str, default_scheme: str = "http") -> str:
    """Normalize common proxy shorthand while preserving explicit URLs."""
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    parts = text.split(":", 3)
    if len(parts) == 4 and parts[0] and _valid_port(parts[1]) and parts[2] and parts[3]:
        host, port, username, password = parts
        return f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if len(parts) == 4 and parts[0] and parts[1] and parts[2] and _valid_port(parts[3]):
        username, password, host, port = parts
        return f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    # urlparse 遇到不成对的 IPv6 方括号会直接抛错；无法识别的写法原样返回，交给调用方校验。
    try:
        parsed = urlparse(f"//{text}")
        port = parsed.port
    except ValueError:
        return text
    if parsed.hostname and port:
        return f"{default_scheme}://{text}"
    return text


def normalize_proxy_list(values, default_scheme: str = "http") -> list[str]:
    """Normalize a multiline proxy list and omit empty entries."""
    if values is None:
        return []
    if isinstance(values, str):
        values = values.splitlines()
    return [
        normalized
        for value in values
        if (normalized := normalize_proxy_url(value, default_scheme=default_scheme))
    ]


def split_proxy_pool_line(line: str) -> tuple[str, str, bool]:
    """拆出代理池单行的 (代理地址, 名称, 是否启用)。

    带协议的 URL 自身不会含未编码的 ``#``，因此第一个 ``#`` 之后就是名称；
    host:port:user:pass 等简写不解析名称，密码里的 ``#`` 保持原有兼容行为。
    """
    text = str(line or "").strip()
    enabled = not text.startswith("#")
    if not enabled:
        text = text[1:].strip()
    name = ""
    if "://" in text and "#" in text:
        text, name = (part.strip() for part in text.split("#", 1))
    return text, name, enabled


def format_proxy_pool_line(url: str, name: str = "", enabled: bool = True) -> str:
    """按 split_proxy_pool_line 的格式写回一行；名称压成单行文本。"""
    text = normalize_proxy_url(url)
    if "://" not in text:
        raise ValueError("代理地址缺少协议或端口")
    name = " ".join(str(name or "").split())
    line = f"{text}#{name}" if name else text
    return line if enabled else f"# {line}"


def proxy_pool_entries(values, default_scheme: str = "http") -> list[dict]:
    """解析代理池配置，返回 [{url, name, enabled, raw}]，包含停用项。"""
    if values is None:
        return []
    if isinstance(values, str):
        values = values.splitlines()
    entries = []
    for value in values:
        raw = str(value or "").strip()
        url, name, enabled = split_proxy_pool_line(raw)
        if not url:
            continue
        entries.append({
            "url": normalize_proxy_url(url, default_scheme=default_scheme),
            "name": name,
            "enabled": enabled,
            "raw": raw,
        })
    return entries


def pick_proxy() -> str:
    """从代理池中随机抽取一个代理 URL；池为空时返回空串（即不使用代理）。"""
    return random.choice(PROXY_POOL) if PROXY_POOL else ""


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_POOL_UPSTREAM_PROXY': 'str',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'list_str_multiline',
    'PLAN_CHECK_UPSTREAM_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
# 运行时只保留启用项的纯 URL（去掉名称），调用方无需感知行格式。
PROXY_POOL = [entry["url"] for entry in proxy_pool_entries(PROXY_POOL) if entry["enabled"]]
PLAN_CHECK_PROXY = normalize_proxy_list(PLAN_CHECK_PROXY)
PROXY = pick_proxy()
