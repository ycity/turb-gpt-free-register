# -*- coding: utf-8 -*-
"""CloakBrowser 自动化注册配置。"""
from config.env_loader import apply_env_overrides

# 是否无头启动：False=显示窗口，True=无头。
CLOAK_HEADLESS: bool = True

# 是否启用 CloakBrowser humanize 行为。
CLOAK_HUMANIZE: bool = True

# 使用当前出口 IP 自动匹配时区/语言/WebRTC IP。
CLOAK_GEOIP: bool = True

# 显式指定 Cloak 语言/时区；留空则在 CLOAK_GEOIP=True 时按出口 IP 自动推断。
# 例如：CLOAK_LOCALE="ja-JP"，CLOAK_TIMEZONE="Asia/Tokyo"。
CLOAK_LOCALE: str = ""
CLOAK_TIMEZONE: str = ""

# 是否把本项目传入/代理池抽取的代理传给 CloakBrowser。
CLOAK_USE_PROXY: bool = True

# Pro license；留空则使用免费 binary。
CLOAK_LICENSE_KEY: str = ""

# 固定指纹 seed；留空则每次 launch 随机生成新指纹。
CLOAK_FINGERPRINT_SEED: str = ""

# 持久化用户目录；留空则临时上下文。若要固定账号画像/缓存，可填如 "./cloak-profiles/default"。
# 注意：这是所有任务共用的单一目录，不随代理区分，也不会清理登录态，仅适合单任务调试。
# 批量注册请改用下面的 CLOAK_PERSIST_PROFILE。
CLOAK_USER_DATA_DIR: str = ""

# 按代理出口复用浏览器资料目录。开启后每个代理一个目录，保留 Cloudflare 放行 cookie
# （cf_clearance 等）和该目录固定的指纹，避免每次注册都要重新通过人机验证。
# 启动时只保留 Cloudflare cookie，其余 cookie 全部清空，不会带着上一个账号的登录态。
CLOAK_PERSIST_PROFILE: bool = False

# 资料目录根路径；留空则使用项目下的 cache/cloak-profiles。
CLOAK_PROFILE_ROOT: str = ""

# 资料目录闲置多少天后自动清理；0 表示不清理。
CLOAK_PROFILE_MAX_IDLE_DAYS: int = 14

# 额外 Chromium 参数，例如 ["--fingerprint=12345"]。CLOAK_FINGERPRINT_SEED 会自动追加。
CLOAK_EXTRA_ARGS: list = []

# 与原 Roxy Selenium 流程共用的超时时间。
CLOAK_SELENIUM_TIMEOUT: int = 90

# 调试时保留浏览器不自动关闭。
CLOAK_KEEP_BROWSER_OPEN: bool = False

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'CLOAK_HEADLESS': 'bool', 'CLOAK_HUMANIZE': 'bool', 'CLOAK_GEOIP': 'bool', 'CLOAK_LOCALE': 'str', 'CLOAK_TIMEZONE': 'str', 'CLOAK_USE_PROXY': 'bool', 'CLOAK_LICENSE_KEY': 'str', 'CLOAK_FINGERPRINT_SEED': 'str', 'CLOAK_USER_DATA_DIR': 'str', 'CLOAK_PERSIST_PROFILE': 'bool', 'CLOAK_PROFILE_ROOT': 'str', 'CLOAK_PROFILE_MAX_IDLE_DAYS': 'int', 'CLOAK_SELENIUM_TIMEOUT': 'int', 'CLOAK_KEEP_BROWSER_OPEN': 'bool'})
