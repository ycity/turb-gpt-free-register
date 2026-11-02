# -*- coding: utf-8 -*-
"""
AiLink VCC 自动支付配置

生成 Team 长链后，用 VCC 虚拟卡自动填写 Stripe 结账页并提交。

资金安全上有两道闸门，都默认开着：
    1. VCC_EXPECTED_AMOUNT —— 页面总额必须等于该值才提交
    2. VCC_MAX_CHARGE      —— 总额超过该值一律不提交

两道闸门任何一道不通过都会停在提交按钮前并截图，不会扣款。
"""
from config.env_loader import apply_env_overrides

# 总开关。关掉则生成长链后不做任何支付动作。
VCC_AUTO_PAY_ENABLED = False

# 凭据。留空时按顺序回退到 VCC_CRED_FILE 指向的文件。
# 文件格式每行 `key: xxx` 或 `key=xxx`，支持 key / secret / webhook_secret。
VCC_API_KEY = ""
VCC_API_SECRET = ""
# 开卡三要素密文用 webhook_secret 派生密钥解密，与 API Secret 不是同一个值。
VCC_WEBHOOK_SECRET = ""
VCC_CRED_FILE = ""

VCC_API_BASE = "https://card.ailinkvcc.com"
VCC_API_TIMEOUT = 30.0

# VCC 开放接口会校验来源 IP 白名单；不在白名单内一律 403。
# 本机调试时若未加白名单，请在已加白的服务器上跑。

# ---- 账单信息 ----
# 必须是卡实际登记的账单地址。这些值会原样提交给 Stripe。
# 不要为了规避税费填写非登记地址。
VCC_BILLING_COUNTRY = "US"
VCC_BILLING_STATE = "AK"
VCC_BILLING_CITY = "Anchorage"
VCC_BILLING_LINE1 = "11901 Northern Raven Dr"
VCC_BILLING_LINE2 = ""
VCC_BILLING_POSTAL = "99516"

# 持卡人姓名来源：
#   holder = 从 cardholders/query 取（按 VCC_HOLDER_ID，未指定则取 merchantDefault，
#            再退而取唯一 ACTIVE 持卡人）
#   config = 直接用 VCC_BILLING_NAME
VCC_NAME_SOURCE = "holder"
VCC_HOLDER_ID = 0
VCC_BILLING_NAME = ""

# ---- 选卡 ----
# 指定卡 ID 时只用这张；留空则取第一张状态正常且余额足够的卡。
VCC_CARD_ID = ""
# 账单币种与卡币种通常不同，这里只做"卡可用余额 >= 该值"的粗筛，单位是卡的币种。
VCC_MIN_CARD_BALANCE = 0.0

# ---- 金额闸门 ----
# 页面"本次应付总额"必须精确等于该值才允许提交。单位与页面币种一致。
# 设为 0 表示不做等值校验（此时仅 VCC_MAX_CHARGE 生效）。
VCC_EXPECTED_AMOUNT = 1150.0
# 期望币种；留空表示不校验币种。
VCC_EXPECTED_CURRENCY = "EGP"
# 允许的绝对误差，用于容忍页面四舍五入。
VCC_AMOUNT_TOLERANCE = 0.01
# 硬上限：无论等值校验是否开启，总额超过该值都不提交。
VCC_MAX_CHARGE = 1200.0

# ---- 提交行为 ----
# True  = 两道闸门都通过后真实点击提交并扣款
# False = 填完停在提交按钮前并截图，不扣款
VCC_AUTO_SUBMIT = True

# 勾选 OpenAI 商务条款复选框。该框是必填项，不勾无法提交。
VCC_ACCEPT_TERMS = True
# 勾选"保存支付信息"，便于后续续订。
VCC_SAVE_PAYMENT_METHOD = False

# ---- 3DS ----
# 轮询 cards/verification-code/query 取 OTP，不需要配置公网 Webhook 地址。
VCC_3DS_ENABLED = True
VCC_3DS_TIMEOUT = 180.0
VCC_3DS_POLL_INTERVAL = 5.0

# ---- 浏览器 ----
VCC_HEADLESS = True
VCC_PAGE_TIMEOUT = 60.0
# 截图目录，留空则用 cache/vcc_pay/
VCC_SCREENSHOT_DIR = ""

# 用本机已装的浏览器而不是 Playwright 自带内核。取值 chrome / msedge / chrome-beta 等。
# 留空则用自带内核；注意自带的是 headless_shell，只能无头跑，开不出窗口。
VCC_BROWSER_CHANNEL = ""
# 直接指定浏览器可执行文件路径（优先级低于 VCC_BROWSER_CHANNEL）。
VCC_BROWSER_PATH = ""

# 填完表单后把窗口交给人，由人自己点提交。开启时强制有头，且绝不自动提交。
VCC_KEEP_OPEN = False
# 交还窗口后最多等多久（秒）。期间会阻塞调用方，WebUI 流程里慎用。
VCC_KEEP_OPEN_TIMEOUT = 1800.0

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'VCC_AUTO_PAY_ENABLED': 'bool',
    'VCC_API_KEY': 'str',
    'VCC_API_SECRET': 'str',
    'VCC_WEBHOOK_SECRET': 'str',
    'VCC_CRED_FILE': 'str',
    'VCC_API_BASE': 'str',
    'VCC_API_TIMEOUT': 'float',
    'VCC_BILLING_COUNTRY': 'str',
    'VCC_BILLING_STATE': 'str',
    'VCC_BILLING_CITY': 'str',
    'VCC_BILLING_LINE1': 'str',
    'VCC_BILLING_LINE2': 'str',
    'VCC_BILLING_POSTAL': 'str',
    'VCC_NAME_SOURCE': 'str',
    'VCC_HOLDER_ID': 'int',
    'VCC_BILLING_NAME': 'str',
    'VCC_CARD_ID': 'str',
    'VCC_MIN_CARD_BALANCE': 'float',
    'VCC_EXPECTED_AMOUNT': 'float',
    'VCC_EXPECTED_CURRENCY': 'str',
    'VCC_AMOUNT_TOLERANCE': 'float',
    'VCC_MAX_CHARGE': 'float',
    'VCC_AUTO_SUBMIT': 'bool',
    'VCC_ACCEPT_TERMS': 'bool',
    'VCC_SAVE_PAYMENT_METHOD': 'bool',
    'VCC_3DS_ENABLED': 'bool',
    'VCC_3DS_TIMEOUT': 'float',
    'VCC_3DS_POLL_INTERVAL': 'float',
    'VCC_HEADLESS': 'bool',
    'VCC_PAGE_TIMEOUT': 'float',
    'VCC_SCREENSHOT_DIR': 'str',
    'VCC_BROWSER_CHANNEL': 'str',
    'VCC_BROWSER_PATH': 'str',
    'VCC_KEEP_OPEN': 'bool',
    'VCC_KEEP_OPEN_TIMEOUT': 'float',
})
