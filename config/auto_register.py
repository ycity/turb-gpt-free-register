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

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'AUTO_REGISTER_ENABLED': 'bool',
    'AUTO_REGISTER_INTERVAL': 'float',
    'AUTO_REGISTER_POLL_INTERVAL': 'float',
    'AUTO_REGISTER_2FA_TIMEOUT': 'float',
})
