# -*- coding: utf-8 -*-
"""
Ant Browser（Launch API 执行节点）配置。

架构：生产服务器是大脑（WebUI/DB/协议流程），Ant Browser 所在 Windows 机是
"浏览器执行节点"。服务器通过 SSH 隧道访问节点的 Launch API（默认 127.0.0.1:19876）
与统一 CDP 入口（同端口，非 /api 路径全部反代到活动实例调试端口）。

Launch API 端点（v1.9.4 实测，源码 backend/internal/launchcode 对齐）：
- GET  /api/health                   健康检查
- GET  /api/profiles                 实例列表
- POST /api/profiles                 创建实例 {profile: ProfileInput, launchCode, autoLaunch, start}
- GET/PUT/DELETE /api/profiles/{id}  实例读/改/删
- GET  /api/profiles/{id}/status     实例运行态（cdpUrl/directDebugUrl/debugPort）
- POST /api/profiles/{id}/stop       停止实例
- POST /api/runtime/session          按 selector 启动并等待 CDP 就绪（timeoutMs ≤ 120000）
- POST /api/runtime/stop             按 selector 停止
- GET  /api/runtime/active           当前活动实例（统一 CDP 入口目标）
- GET  /api/launch/{code}            按 launchCode 启动
- 非 /api 路径                        统一 CDP 反向代理 → 活动实例调试端口

安全：LaunchServer 只绑定 127.0.0.1，且 localhostMiddleware 拒绝非本机来源；
远程访问必须走 SSH 隧道/WireGuard，绝不能裸暴露公网。可选 API Key（X-Ant-Api-Key）。
"""
from config.env_loader import env_str, apply_env_overrides

# 驱动类型扩展：高敏操作（补密码/2FA/换卡）默认走 ant 浏览器节点
# （协议通道对 CF 指纹级 403 无解；见 HANDOFF-ANTBROWSER.md）
SENSITIVE_OPS_DRIVER: str = env_str("SENSITIVE_OPS_DRIVER", "ant")

# Ant Browser Launch API 地址。
#   本机开发: http://127.0.0.1:19876
#   生产服务器经隧道: 同样是 127.0.0.1:19876（隧道把远端 19876 映射到本机）
ANT_API_BASE: str = env_str("ANT_API_BASE", "http://127.0.0.1:19876")

# Launch API 可选鉴权（Ant 设置页开启 API 认证后填写；不填则不带头）
ANT_API_KEY: str = env_str("ANT_API_KEY", "")

# API Key 请求头名（默认 X-Ant-Api-Key，一般不用改）
ANT_API_KEY_HEADER: str = env_str("ANT_API_KEY_HEADER", "X-Ant-Api-Key")

# 固定实例 ID（调试用）。留空且开启一号一实例时，每轮自动创建新实例。
ANT_PROFILE_ID: str = env_str("ANT_PROFILE_ID", "")

# 一号一实例：每个账号强制创建新实例（高敏操作必须复用注册时的 profile，
# 若账号已绑定 profileId，由调用方显式传入，不走这里的自动创建）。
ANT_ONE_PROFILE_PER_ACCOUNT: bool = env_str("ANT_ONE_PROFILE_PER_ACCOUNT", "True").lower() in ("1", "true", "yes", "on")

# 任务结束清理
ANT_KEEP_BROWSER_OPEN: bool = env_str("ANT_KEEP_BROWSER_OPEN", "False").lower() in ("1", "true", "yes", "on")
ANT_DELETE_PROFILE_AFTER_RUN: bool = env_str("ANT_DELETE_PROFILE_AFTER_RUN", "True").lower() in ("1", "true", "yes", "on")

# 新建实例名前缀（实例名 = 前缀-毫秒时间戳-随机hex）
ANT_PROFILE_NAME_PREFIX: str = env_str("ANT_PROFILE_NAME_PREFIX", "ant")

# 实例绑定的内核 coreId（Ant 内核管理中注册的 ID）。
# fp-chromium-148：fingerprint-chromium 150 有 canvas 回读崩溃 bug（上游 issue #94/#95/#104），
# 已降级到 148.0.7778.215；WebGL 字符串伪装在 148 上正常。
ANT_CORE_ID: str = env_str("ANT_CORE_ID", "")

# 默认实例标签，便于在 Ant UI 里识别本项目创建的实例
ANT_PROFILE_TAGS: str = env_str("ANT_PROFILE_TAGS", "turb-gpt")

# 启动等待 CDP 就绪的超时（毫秒；Ant 上限 120000）
ANT_RUNTIME_SESSION_TIMEOUT_MS: int = int(env_str("ANT_RUNTIME_SESSION_TIMEOUT_MS", "60000"))

# 传入内核的额外启动参数：软件渲染 WebGL（VM 无 GPU 时 WebGL 会整个消失，
# 是比 SwiftShader 更强的特征；开启后内核会把 WebGL 字符串伪装成真实 GPU）
ANT_EXTRA_LAUNCH_ARGS: str = env_str(
    "ANT_EXTRA_LAUNCH_ARGS",
    "--use-gl=angle,--use-angle=swiftshader,--enable-unsafe-swiftshader",
)

# 请求重试
ANT_API_RETRIES: int = int(env_str("ANT_API_RETRIES", "3"))
ANT_API_RETRY_DELAY: float = float(env_str("ANT_API_RETRY_DELAY", "2"))
ANT_API_TIMEOUT: int = int(env_str("ANT_API_TIMEOUT", "90"))

# 创建实例请求错峰间隔（秒），多 worker 并发时避免同刻创建
ANT_CREATE_INTERVAL: float = float(env_str("ANT_CREATE_INTERVAL", "1.0"))

# 调试截图目录（高敏操作关键步骤留证；留空则不截图）
ANT_DEBUG_SHOT_DIR: str = env_str("ANT_DEBUG_SHOT_DIR", "./cache/ant-shots")

# CDP 地址重写：跨机驱动时，Ant 返回的 127.0.0.1:<port> 需要映射为
# 驱动侧可见的 host。格式 "host" 或 "host:portBase"，留空表示本机驱动不重写。
# 例：生产服务器侧配 ANT_CDP_REWRITE_HOST="127.0.0.1"（端口经隧道转发保持一致）。
ANT_CDP_REWRITE_HOST: str = env_str("ANT_CDP_REWRITE_HOST", "")

# 代理：实例级代理 URL（http/https/socks5[5h]://user:pass@host:port 或 direct://）。
# 留空则跟随注册流程/调用方传入；Ant 侧由 xray/sing-box 桥接驱动。
ANT_PROFILE_PROXY_URL: str = env_str("ANT_PROFILE_PROXY_URL", "")

# Ant 节点不可达的出口：逗号/空白分隔，按子串匹配代理 URL（填出口的主机名或 IP）。
# 代理池是多流程共用的——有的出口从本机（协议流程）通、从 HK 节点 TCP 不通。
# 注册驱动从池里抽代理时跳过这些条目；其它流程照常使用，不受影响。
ANT_PROXY_EXCLUDE: str = env_str("ANT_PROXY_EXCLUDE", "")

apply_env_overrides(globals())
