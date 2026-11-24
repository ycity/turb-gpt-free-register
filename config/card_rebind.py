# -*- coding: utf-8 -*-
"""
一键换绑支付卡配置

给已订阅账号换绑付款卡：登录 OpenAI 管理后台（admin.openai.com 工作区
账单页）→ 添加新卡（Stripe Elements + hCaptcha Enterprise 自动打码）→
设为默认 → 删除旧卡。默认换绑到卡片黑名单（AUTO_SUBSCRIBE_CARD_BLACKLIST）
里的牺牲卡。
"""
from config.env_loader import apply_env_overrides

# 换绑网络代理模式：
#   saved = 优先使用账号保存的有效代理（无有效代理时回退代理池）
#   pool  = 忽略账号保存的代理，每次任务都从 PROXY_POOL 随机抽取
CARD_REBIND_PROXY_MODE = "saved"

# 换绑目标卡：留空 = 自动取 AUTO_SUBSCRIBE_CARD_BLACKLIST 黑名单里第一张
# 可用的 VCC 卡（牺牲卡）；也可填具体卡 ID（VCC 卡 ID 或 local:N 自有卡）。
CARD_REBIND_CARD_ID = ""

# 换绑后台队列。worker 是实际同时执行的账号数；换绑走 CloakBrowser 重进程，
# 并发过高会挤爆容器内存。
CARD_REBIND_WORKERS = 1
CARD_REBIND_QUEUE_LIMIT = 50

# 每个阶段（SSO/加卡/删卡）的页面等待上限（秒）。
CARD_REBIND_PAGE_TIMEOUT = 60

# 加卡 hCaptcha 自动打码失败后是否转人工（远程接管），False 则直接失败。
CARD_REBIND_ALLOW_HUMAN = True

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'CARD_REBIND_PROXY_MODE': 'str',
    'CARD_REBIND_CARD_ID': 'str',
    'CARD_REBIND_WORKERS': 'int',
    'CARD_REBIND_QUEUE_LIMIT': 'int',
    'CARD_REBIND_PAGE_TIMEOUT': 'int',
    'CARD_REBIND_ALLOW_HUMAN': 'bool',
})
