# -*- coding: utf-8 -*-
"""
注册基础信息（默认值）

CLI 走 main.py 时会优先读这里；Web 控制台批量注册时也会用同样的默认值。
留空字段会触发交互式输入或自动生成（仅 USE_EMAIL_SERVICE=True 时邮箱会从 Outlook 池领取）。
"""
from config.env_loader import apply_env_overrides

# 注册邮箱（留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取）
REGISTER_EMAIL = ""

# 注册密码（OTP-only 流程已不需要，留作备用）
REGISTER_PASSWORD = ""

# 用户名（注册完成后设置的显示名称，留空会自动生成 "Foo Bar" 形式）
# OpenAI 限制：name_invalid_chars —— 只允许字母和空格
REGISTER_NAME = ""

# 注册成功落库后是否自动查询套餐/Plus 资格。
# 关闭后不会在注册完成后立刻访问 backend-api/accounts/check，后续可在账号列表手动查询。
AUTO_PLAN_CHECK_AFTER_REGISTER = False

# 纯协议注册指纹生命周期：
#   False = 每次注册任务创建全新的 device/session/浏览器画像（默认）
#   True  = 同一邮箱重复注册或补跑时复用稳定指纹；不同邮箱仍彼此独立
PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL = False

# 注册成功并拿到 accessToken 后，在浏览器里随机停留一段时间再关闭连接。
# 格式：最小秒,最大秒。设为 "0,0" 表示不额外停留。
POST_REGISTER_DWELL_SECONDS_RANGE = "5,15"

# 提链时尝试只买 1 个 Team 席位（省一半订阅费）。
# 服务端对部分账号放开了 1 席（business_minimum_seats=1），实测约两成命中，
# 按账号固定、不是随机。不支持的账号自动退回 2 席，不影响原流程。
# 注意 1 席只能走 custom 模式，拿到的是 chatgpt.com/checkout/... 而不是
# pay.openai.com 长链，VCC 自动支付的填表逻辑未必适配，必要时关掉它。
TEAM_TRY_SINGLE_SEAT = True

# 2FA 绑定成功后顺带探测该账号的 Team 单席位及结账折扣（首月免费 / 具体折扣比例）。
# 只创建 checkout session 读限制与折扣，不支付、不消耗优惠码。
TEAM_PROBE_SINGLE_SEAT_AFTER_2FA = True

# 注册落库后自动探测 Team 单席位及结账折扣，不等 2FA。
#
# 席位与折扣探测只用 access_token 直连 API，和 2FA 本来没有任何关系。但它以前
# 只挂在 2FA 成功之后，于是 2FA 一关或一失败，席位与折扣信息就永远拿不到——
# 2026-11-17 关掉自动 2FA 做对照实验时撞上过：账号注册正常、人也活着，
# 席位却查不出来。
#
# 两条路径并存，按"是否已有成功结果"去重：2FA 那条用的是刚刷新的新 token，
# 更准，所以保留；这条兜住"没有 2FA"或 2FA 探测失败的情况。
PROBE_SINGLE_SEAT_AFTER_REGISTER = True

# 注册后等多少秒再探测单席位与折扣。
#
# 不能一落库就探：账号刚建好，OpenAI 风控还在观察期，立刻去创建 checkout
# session 会被判 "unusual activity"（HTTP 400）——2026-11-17 账号 #105
# 实测，入库后 2 秒探测直接被拒。
#
# 以前走 2FA 那条路时天然隔了 TWOFA_REGISTRATION_DELAY（180 秒）才探，
# 所以从没撞上。这里取同一个数量级，让两条路径的节奏一致。
PROBE_SINGLE_SEAT_DELAY = 180.0

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'REGISTER_EMAIL': 'str',
    'REGISTER_NAME': 'str',
    'AUTO_PLAN_CHECK_AFTER_REGISTER': 'bool',
    'PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL': 'bool',
    'POST_REGISTER_DWELL_SECONDS_RANGE': 'str',
    'TEAM_TRY_SINGLE_SEAT': 'bool',
    'TEAM_PROBE_SINGLE_SEAT_AFTER_2FA': 'bool',
    'PROBE_SINGLE_SEAT_AFTER_REGISTER': 'bool',
    'PROBE_SINGLE_SEAT_DELAY': 'float',
})
