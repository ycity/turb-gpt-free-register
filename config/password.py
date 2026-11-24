# -*- coding: utf-8 -*-
"""
账号补密码（忘记密码邮件流）配置

给 CloakBrowser 注册的无密码账号补设 ChatGPT 登录密码：
    auth.openai.com 发起重置 → 邮箱收验证码 → 校验 → 设置新密码 → 存库。
    补上密码后查活登录自动优先走"密码+TOTP"，不再依赖容易被风控丢弃的邮箱 OTP。
"""
from config.env_loader import apply_env_overrides

# 补密码网络代理模式：
#   saved = 优先使用账号保存的有效代理（无有效代理时回退代理池）
#   pool  = 忽略账号保存的代理，每次任务都从 PROXY_POOL 随机抽取
PASSWORD_BACKFILL_PROXY_MODE = "saved"

# 等待重置验证码邮件的最长秒数（重置邮件通常几秒内到达，风控时限流会慢）。
PASSWORD_BACKFILL_EMAIL_TIMEOUT = 120

# 设置密码成功后是否立即用"密码+TOTP"登录验证一次（顺带刷新 access_token）。
PASSWORD_BACKFILL_VERIFY_LOGIN = True

# 补密码后台队列。workers 是实际同时执行的账号数，修改后需重启进程以重建线程池。
PASSWORD_BACKFILL_WORKERS = 2
PASSWORD_BACKFILL_QUEUE_LIMIT = 100

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PASSWORD_BACKFILL_PROXY_MODE': 'str',
    'PASSWORD_BACKFILL_EMAIL_TIMEOUT': 'int',
    'PASSWORD_BACKFILL_VERIFY_LOGIN': 'bool',
    'PASSWORD_BACKFILL_WORKERS': 'int',
    'PASSWORD_BACKFILL_QUEUE_LIMIT': 'int',
})
