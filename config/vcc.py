# -*- coding: utf-8 -*-
"""
AiLink VCC 自动支付配置

生成 Team 长链后，用 VCC 虚拟卡自动填写 Stripe 结账页并提交。

资金安全上的闸门（三道，当前全部关闭）：
    1. VCC_MAX_CHARGE      —— 总额超过该值一律不提交
    2. VCC_EXPECTED_AMOUNT —— 总额必须等于该值才提交
    3. VCC_EXPECTED_CURRENCY —— 页面币种必须等于该值才提交

为什么全关：EG（EGP，千量级）和 US（USD，十量级）两条路都在用，
任何一个写死的数或币种都会把另一边的正常单拦死。这是明确选定的取舍，
代价是自动支付不再对金额做拦截——靠人在"等待确认"那步核对。

还在生效的保护：
    - 单席位路径会核对页面席位数，大于 1 直接停
    - 卡里余额有限，备的钱不够时扣款自然失败
充值金额由 VCC_FUNDING_USD 决定（置 0 时回落到 VCC_FUNDING_AMOUNT 按汇率
折算），与校验无关。
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

# 支付前按卡号查该卡登记的账单地址（GET https://ssr.im/address?id={卡号}）。
#
# 为什么要查：VCC 每张卡绑定的账单地址不同，而下面 VCC_BILLING_* 只能写死
# 一份。地址和卡对不上时 Stripe 直接拒付，还白耗一个长链和一个优惠码。
# 姓名也用接口返回的 full_name——它和地址同源；VCC 后台的 holderName
# 可能是另一个名字（实测 0363 卡：接口 Clay Foy，VCC Sam Phoenix）。
#
# 关掉则退回用下面写死的 VCC_BILLING_*，适合接口挂掉时应急。
# 只影响 VCC 托管卡；自有卡池的卡仍用卡池里填的信息。
VCC_FETCH_CARD_ADDRESS = True

# 金额闸门通过后自动提交，不等人点确认。
#
# 给自动订阅用：闸门已经校验过页面总额和币种（见 VCC_MAX_CHARGE /
# VCC_EXPECTED_CURRENCY），再让人点一次并没有增加任何判断，只会让流程
# 卡在"等待确认"——实测等 10 分钟后会话被回收，整单白跑。
#
# 闸门没通过时绝不自动提交，仍然停下等人处理。
# 手动订阅不受影响：那条路会传入自己的配置，默认 False。
VCC_AUTO_CONFIRM = False

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
# 每次充值金额（USD）。实际会取它和"刚好够付"中的较大值。
# 跟着备款需求（下面的 VCC_FUNDING_USD = 18）走，略高一点留出零头：
# 低于它的话空卡要按整数倍充两笔，白白在卡里积压一倍的钱；
# 高太多则是把账户的钱提前锁进卡里，账户余额本来就不宽裕。
VCC_RECHARGE_AMOUNT = 20.0
# 等充值到账的上限（秒）。同步响应只表示受理，钱要几秒后才到卡上。
VCC_RECHARGE_TIMEOUT = 90.0
# EGP 兑 USD 的折算汇率，用于判断"卡里的钱够不够付这张 EGP 账单"。
# 写死而不实时查：实时汇率会让判断每次都不一样，排查时说不清。
VCC_USD_RATE = 56.0
# 折算后额外留的余量（USD），覆盖汇率波动和可能的跨境费。
VCC_BALANCE_BUFFER = 0.5

# ---- 金额闸门 ----
# 页面"本次应付总额"必须精确等于该值才允许提交。单位与页面币种一致。
# 设为 0 表示不做等值校验（此时仅 VCC_MAX_CHARGE 生效）。
# 默认关闭：不同优惠码折扣力度不同，写死一个数会把正常的折扣单也拦掉。
VCC_EXPECTED_AMOUNT = 0.0
# 期望币种；留空表示不校验币种。
#
# 留空是明确的选择：现在 EG（EGP，千量级）和 US（USD，十量级）两条路
# 都在用，写死任一个都会把另一个的正常单拦死。单席位走美区更容易付通
# （卡是美国卡，账单国家和卡一致），但埃及区定价更便宜，两边都要能跑。
VCC_EXPECTED_CURRENCY = ""
# 允许的绝对误差，用于容忍页面四舍五入。
VCC_AMOUNT_TOLERANCE = 0.01
# 硬上限：无论等值校验是否开启，总额超过该值都不提交。
#
# 设为 0（关闭）同样是明确的选择。这一个值没法同时管住两种币种：
# 按 EGP 定（2350）对 USD 单形同虚设，按 USD 定（60）又会拦死所有
# EGP 单（最便宜的也要 920）。
#
# 代价说清楚：三道金额闸门现在全关，自动支付不再对金额做任何拦截，
# 多少钱都会提交。防线只剩两条——
#   1. 单席位路径会核对页面席位数，大于 1 直接停（见 _fill_elements_checkout）
#   2. 人工在"等待确认"那一步看金额再点提交
# 卡里余额本身也是兜底：备的钱不够时扣款会失败。
VCC_MAX_CHARGE = 0.0
# 按这个金额准备卡里的钱（USD）。只管充值，不参与金额校验。
# 填 >0 时直接用它，不走下面 VCC_FUNDING_AMOUNT 的 EGP 折算。
#
# 为什么加这个：原本只有 EGP 的备款额，要按汇率折成 USD（1932/56+0.5=35）。
# 那个数是给 EG 区两席位账单备的，而现在主力是 US 区单席位——#83 实际只
# 扣了 17.17 USD，备 35 等于把一倍的钱白锁在卡里，还会把一张余额 34.81、
# 明明够付的卡判成"不足"（线上就这么卡住过）。
#
# 定 18 的依据：#83 实扣 17.17，留 0.83 覆盖税费和汇率零头。
# 单位就是卡的币种 USD，不折算、不乘汇率，所见即所得。
#
# 要回到 EG 两席位那条路，把这里置 0，就会退回用 VCC_FUNDING_AMOUNT 折算。
VCC_FUNDING_USD = 18.0

# 按这个金额准备卡里的钱（EGP）。只管充值，不参与金额校验。
# 仅在上面的 VCC_FUNDING_USD 置 0 时才生效（EG 区两席位那条路）。
# 1932 EGP ÷ 56 + 0.5 = 35.00 USD，即每单备 35 刀。
#
# 注意这个数低于未打折原价 2300：常态的八折单 1840 EGP（32.86 USD）够付，
# 但优惠码失效时页面是原价 2300 EGP（41.07 USD），卡里会不够、扣款失败。
# 这是明确选定的取舍——不想为小概率的原价单常备更多钱，那种情况人工处理。
# 要改回"任何情况都够付"，把这里设成 2300。
VCC_FUNDING_AMOUNT = 1932.0

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

# 单个表单字段操作（fill / select / check）的超时，秒。
# 这类操作一旦被遮挡就注定失败，代码会立刻退回 DOM 直写，所以没必要用
# VCC_PAGE_TIMEOUT 那 60 秒去等：表单十几个字段，几个被遮挡就是好几分钟。
# 页面导航、等表单出现这些仍然用 VCC_PAGE_TIMEOUT。
VCC_FIELD_TIMEOUT = 6.0

# 等支付表单出现的超时，秒。
# 页面本身 1 秒内就能加载完，表单正常也就随后 1-2 秒渲染出来。给 60 秒只会让
# 失效长链白等满一分钟；过期的长链不报错，只是表单永远不出现。
# 调大它对慢网络没什么帮助——真正的慢在页面导航，那一步用 VCC_PAGE_TIMEOUT。
VCC_FORM_TIMEOUT = 12.0
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
VCC_SESSION_TIMEOUT = 2700.0
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

# 支付走 1024proxy 出口时，代理的保留时长（分钟）。
#
# 为什么要调长：1024proxy 默认 time=10。提链只是几个 HTTP 请求，10 分钟
# 绰绰有余；但支付要先登录拿 cookie、填卡、填地址、再等人点人机验证，
# 一单下来常常超过 10 分钟。代理中途失效后，后面每一步都会以各种
# 莫名其妙的方式失败（卡在等表单、读不到金额），很难看出根因是代理过期。
#
# 实测接口接受到 1440（24 小时）。设 60 分钟是留足余量又不长期占用额度。
VCC_1024PROXY_MINUTES = 60

# 支付成功后自动查一次套餐，确认 Team 是否真的生效。
# 支付提交成功不等于套餐立刻落库，OpenAI 侧有处理延迟，所以要等一会儿再查。
VCC_AUTO_PLAN_CHECK = True
VCC_AUTO_PLAN_CHECK_DELAY = 30.0

# ---- 打码：Stripe 的 hCaptcha Enterprise ----
#
# 单席位结账页和「加卡」页在提交时都会弹 hCaptcha Enterprise，并带 rqdata
# （Stripe 把验证码和当次 SetupIntent/PaymentIntent 绑在一起的签名）。自动
# 流程过不了这关就只能停在 awaiting_human 等人点。
#
# 用 2Captcha：in.php 提交任务、res.php 轮询取 token。token 约 2 分钟有效，
# 必须立刻注入同一个浏览器——rqdata 每次会话现生成，不能预先批量解。
#
# 关掉（默认）时行为完全不变：仍停在验证码界面等人手动点。
CAPTCHA_SOLVER_ENABLED = False
CAPTCHA_2CAPTCHA_KEY = ""
# 单次解题最长等待秒数。Stripe 这类 Enterprise 任务偶尔要 1-2 分钟。
CAPTCHA_SOLVE_TIMEOUT = 180.0
# 轮询间隔；2Captcha 建议 5 秒，别打太密。
CAPTCHA_POLL_INTERVAL = 5.0

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'VCC_AUTO_PAY_ENABLED': 'bool',
    'VCC_API_KEY': 'str',
    'VCC_API_SECRET': 'str',
    'VCC_WEBHOOK_SECRET': 'str',
    'VCC_CRED_FILE': 'str',
    'VCC_API_BASE': 'str',
    'VCC_API_TIMEOUT': 'float',
    'VCC_FETCH_CARD_ADDRESS': 'bool',
    'VCC_AUTO_CONFIRM': 'bool',
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
    'CAPTCHA_SOLVER_ENABLED': 'bool',
    'CAPTCHA_2CAPTCHA_KEY': 'str',
    'CAPTCHA_SOLVE_TIMEOUT': 'float',
    'CAPTCHA_POLL_INTERVAL': 'float',
    'VCC_AMOUNT_TOLERANCE': 'float',
    'VCC_MAX_CHARGE': 'float',
    'VCC_FUNDING_USD': 'float',
    'VCC_FUNDING_AMOUNT': 'float',
    'VCC_AUTO_SUBMIT': 'bool',
    'VCC_ACCEPT_TERMS': 'bool',
    'VCC_SAVE_PAYMENT_METHOD': 'bool',
    'VCC_3DS_ENABLED': 'bool',
    'VCC_3DS_TIMEOUT': 'float',
    'VCC_3DS_POLL_INTERVAL': 'float',
    'VCC_HEADLESS': 'bool',
    'VCC_PAGE_TIMEOUT': 'float',
    'VCC_FIELD_TIMEOUT': 'float',
    'VCC_FORM_TIMEOUT': 'float',
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
    'VCC_1024PROXY_MINUTES': 'int',
    'VCC_AUTO_PLAN_CHECK': 'bool',
    'VCC_AUTO_PLAN_CHECK_DELAY': 'float',
})
