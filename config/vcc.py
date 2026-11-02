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
VCC_BILLING_LINE1 = "750 West Dimond Boulevard"
VCC_BILLING_LINE2 = ""
VCC_BILLING_POSTAL = "99515"

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

# ---- 余额检查与自动充值 ----
# 卡内余额不足时自动从 VCC 账户划钱到卡上。
VCC_AUTO_RECHARGE = True
# 每次充值金额（USD）。实际会取它和"刚好够付+1"中的较大值。
VCC_RECHARGE_AMOUNT = 21.0
# 等充值到账的上限（秒）。同步响应只表示受理，钱要几秒后才到卡上。
VCC_RECHARGE_TIMEOUT = 90.0
# EGP 兑 USD 的折算汇率，用于判断"卡里的钱够不够付这张 EGP 账单"。
# 写死而不实时查：实时汇率会让判断每次都不一样，排查时说不清。
VCC_USD_RATE = 56.0
# 折算后额外留的余量（USD），覆盖汇率波动和可能的跨境费。
VCC_BALANCE_BUFFER = 1.0

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

# ---- WebUI 会话式支付 ----
# 浏览器跑在服务端，填好表单后截图给人看，人点确认后由程序提交。
# 会话最长存活时间（秒）：等待确认和等待 3DS 验证码都用这个上限。
VCC_SESSION_TIMEOUT = 900.0
# 点下提交后等页面落定的上限（秒）。Stripe 的处理中可能持续数分钟，
# 超时只报告"结果未知"，绝不重复提交。
VCC_SUBMIT_TIMEOUT = 180.0

# 服务端浏览器用有头模式（需要 Xvfb 虚拟显示）。
# 提交时 Stripe 可能弹 hCaptcha，无头浏览器几乎必被拦；有头 + 真人远程点击
# 才过得去。关掉则退回无头，适合只填表不提交的场景。
VCC_SERVER_HEADED = True
# Xvfb 显示号，容器启动时会在该编号上拉起虚拟显示。
VCC_DISPLAY = ":99"
# 远程交互：把服务端浏览器画面推到网页上，由人直接点击操作。
# 人机验证只认真实交互环境，答案无法从别处搬运，所以必须让人操作这个浏览器。
VCC_REMOTE_CONTROL = True
# 推帧间隔（秒）。越小越跟手，但占带宽和 CPU。
VCC_FRAME_INTERVAL = 0.6

# 用 CloakBrowser 代替裸 Playwright 打开支付页。
# 裸 Chromium 会被 Stripe 风控和发卡行 3DS 判成自动化环境，表现为
# "We are unable to authenticate your payment method"。CloakBrowser 带指纹
# 伪装、真实鼠标轨迹和按出口 IP 匹配的时区语言，注册流程靠它过 Cloudflare。
VCC_USE_CLOAK = True
# 拟人化鼠标/输入轨迹。人机验证和 3DS 风控都会看这个。
VCC_CLOAK_HUMANIZE = True
# 按出口 IP 自动匹配时区、语言、WebRTC，避免代理在美国而浏览器报日本时区。
VCC_CLOAK_GEOIP = True
# 指纹种子，留空则每次随机。填固定值可让同一张卡每次呈现同一台设备。
VCC_CLOAK_SEED = ""
# 填表和提交是否走账号注册时用的代理。关掉则用服务器出口直连。
# 注意：取卡号走 VCC 接口，始终直连以满足 IP 白名单，不受此开关影响。
VCC_USE_ACCOUNT_PROXY = True

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
    'VCC_AUTO_RECHARGE': 'bool',
    'VCC_RECHARGE_AMOUNT': 'float',
    'VCC_RECHARGE_TIMEOUT': 'float',
    'VCC_USD_RATE': 'float',
    'VCC_BALANCE_BUFFER': 'float',
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
    'VCC_SESSION_TIMEOUT': 'float',
    'VCC_SUBMIT_TIMEOUT': 'float',
    'VCC_SERVER_HEADED': 'bool',
    'VCC_DISPLAY': 'str',
    'VCC_REMOTE_CONTROL': 'bool',
    'VCC_FRAME_INTERVAL': 'float',
    'VCC_USE_CLOAK': 'bool',
    'VCC_CLOAK_HUMANIZE': 'bool',
    'VCC_CLOAK_GEOIP': 'bool',
    'VCC_CLOAK_SEED': 'str',
    'VCC_USE_ACCOUNT_PROXY': 'bool',
})
