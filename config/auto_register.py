# -*- coding: utf-8 -*-
"""
自动注册调度配置。

开启后由后台单线程按固定间隔发起注册，每次只跑一个号；邮箱池空了就停。
间隔从上一个号 2FA 结束的时刻开始算（见 AUTO_REGISTER_INTERVAL）。
"""
from config.env_loader import apply_env_overrides

# 总开关。关闭时调度线程仍在跑，但不会发起任何注册。
AUTO_REGISTER_ENABLED = False

# 两次注册的间隔（秒）。计时起点是上一个号 2FA 结束（成功或失败）的时刻，
# 不是注册任务结束的时刻——2FA 由后台队列异步执行，还带注册后冷却。
AUTO_REGISTER_INTERVAL = 1800.0

# 调度线程的检查间隔（秒）。只影响发起时机的精度。
AUTO_REGISTER_POLL_INTERVAL = 30.0

# 2FA 最长等待（秒）。超过这个时间仍停在 queued/running 就不再等它，
# 否则 2FA 卡死会让自动注册永久停摆。
AUTO_REGISTER_2FA_TIMEOUT = 1800.0

# 注册失败后多久重试（秒）。
#
# 为什么要单独配：AUTO_REGISTER_INTERVAL 是"成功一个号之后歇多久"，
# 用来控制注册频率、避免被风控。但注册失败时根本没建成号，再等半小时
# 纯属浪费——失败往往是出口被挡、邮箱已废这类换一组就能解决的问题。
# 失败后换出口+换邮箱立刻重试，效率差别很大。
#
# 注意这不会削弱风控节流：失败意味着 OpenAI 侧没有新账号产生。
AUTO_REGISTER_RETRY_INTERVAL = 120.0

# 连续失败多少次后回落到正常间隔。
#
# 防的是"出口全废/邮箱池全是坏号"这类系统性故障：那种情况下快速重试
# 只会以 2 分钟一次的频率持续烧号，不如慢下来等人介入。
AUTO_REGISTER_MAX_FAST_RETRIES = 3

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'AUTO_REGISTER_ENABLED': 'bool',
    'AUTO_REGISTER_INTERVAL': 'float',
    'AUTO_REGISTER_POLL_INTERVAL': 'float',
    'AUTO_REGISTER_2FA_TIMEOUT': 'float',
    'AUTO_REGISTER_RETRY_INTERVAL': 'float',
    'AUTO_REGISTER_MAX_FAST_RETRIES': 'int',
})
