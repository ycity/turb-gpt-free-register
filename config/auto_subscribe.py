# -*- coding: utf-8 -*-
"""自动订阅配置：定时给单席位账号开 Team。

每到计划时刻检查一次账号池，发现"支持单席位但还没订阅"的号就自动走
完整订阅流程（随机选卡 -> 按卡号查账单地址 -> 生成长链 -> 支付）。

默认关闭：这会真实扣款，必须由人显式打开。
"""
from config.env_loader import apply_env_overrides

# 总开关。关掉则调度线程照常跑，但什么都不做。
#
# 默认 False 是刻意的：订阅会真金白银扣款且不可逆，不能因为部署了新版本
# 就自动开始花钱。确认流程跑通后再打开。
AUTO_SUBSCRIBE_ENABLED = False

# 检查时刻（新加坡时间 UTC+8 的整点），逗号分隔。
#
# 为什么用固定时刻而不是固定间隔：扣款要可预期，人得能知道"下一次可能
# 什么时候扣钱"。固定间隔跑几天就漂了，说不清下一次在什么时候。
#
# 写成字符串而不是列表：.env 覆盖只支持 bool/int/float/str/多行列表，
# 没有整数列表类型；逗号分隔最直观，解析时会忽略空白和非法值。
AUTO_SUBSCRIBE_HOURS = "10,12,14,16,18,20"

# 结算国家。出口国家必须和结算货币同区，否则 Stripe 直接拒付
#（实测 EGP 账单配美国卡会被发卡行拒）。
#
# EG/EGP 是当前在用的组合：埃及区单席位 1150 EGP，八折后 920 EGP，
# 比美区便宜。支付出口会按这里的国家现取 1024proxy 埃及节点。
AUTO_SUBSCRIBE_COUNTRY = "EG"

# 单次支付金额上限（按结算货币计）。超过就不付。
#
# 为什么要这道闸门：优惠码失效时页面会变成原价（EG 区 1150 EGP），
# 自动订阅没人盯着，很容易按原价扣掉。920 = 1150 × 0.8，正好是八折价，
# 高于它说明优惠码没生效，该停下来查而不是照付。
#
# 单位跟随 AUTO_SUBSCRIBE_COUNTRY 对应的货币，改结算国家时要一起改。
AUTO_SUBSCRIBE_MAX_AMOUNT = 920.0

# 新建 Team 工作区的名称。
AUTO_SUBSCRIBE_WORKSPACE = "MyTeam"

# 卡片黑名单：这些卡不参与随机选卡，逗号分隔的卡号后四位。
#
# 用途：某张卡被 Stripe 风控标记、或留作他用、或余额是给别的流程备的，
# 都不该被自动订阅随机抽到。按后四位匹配，不用填完整卡号。
#
# 5931：用户点名拉黑（余额 0.01，且用于换卡功能验证）。
# 8361：2026-11-18 #110 用它付款被 Stripe 拒（"Payment was not approved"），
#       余额 23.36 充足、出口也是 EG，仍被拒 → 判定该卡已被 Stripe 风控标记。
AUTO_SUBSCRIBE_CARD_BLACKLIST = "5931,8361"

# 账户低余额阈值（USD）。
#
# 当 VCC 账户可用余额低于该值时（账户已不足以给低余额卡充值），选卡策略从
# "全量卡均衡"切换为"优先从卡内余额充足（>= 该值）的卡里挑使用次数最少的"；
# 账户余额 >= 该值时仍按全量卡均衡选卡，保持单卡用量分散。
# 设为 0 表示关闭该策略。
AUTO_SUBSCRIBE_LOW_BALANCE_USD = 18.0

# 调度线程多久醒一次（秒）。整点判断精确到小时，一分钟足够。
AUTO_SUBSCRIBE_POLL_INTERVAL = 60.0

# 支付失败后的最小重试间隔（分钟）。
#
# 失败（如出口被风控）不再占用整点，调度线程每分钟都醒着；没有这道闸
# 就等于每分钟重新发起一次——每次都要拉出口、提链、登录，白烧流量。
# 15 分钟 ≈ 熔断冷却时长，风控解除通常也在这个量级。
AUTO_SUBSCRIBE_RETRY_INTERVAL_MIN = 15

# 同步等支付结果的上限（秒）。各等待环节自身都有超时（会话级 900s），
# 这里只是兜底：超过就按"已发起、结果未知"返回，不无限阻塞调度线程。
AUTO_SUBSCRIBE_PAY_WAIT_SEC = 1800.0

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'AUTO_SUBSCRIBE_ENABLED': 'bool',
    'AUTO_SUBSCRIBE_HOURS': 'str',
    'AUTO_SUBSCRIBE_COUNTRY': 'str',
    'AUTO_SUBSCRIBE_MAX_AMOUNT': 'float',
    'AUTO_SUBSCRIBE_WORKSPACE': 'str',
    'AUTO_SUBSCRIBE_CARD_BLACKLIST': 'str',
    'AUTO_SUBSCRIBE_LOW_BALANCE_USD': 'float',
    'AUTO_SUBSCRIBE_POLL_INTERVAL': 'float',
    'AUTO_SUBSCRIBE_RETRY_INTERVAL_MIN': 'int',
    'AUTO_SUBSCRIBE_PAY_WAIT_SEC': 'float',
})
