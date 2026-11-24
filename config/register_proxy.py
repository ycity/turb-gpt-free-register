# -*- coding: utf-8 -*-
"""注册出口配置：用 1024proxy 现取，随机国家。

为什么换掉固定代理池：池里那些长期出口被反复用于注册，IP 已经被盯上。
2026-11-17 实测 #85 之后注册的 17 个号，确认 9 个 account_deactivated、
存活 0。1024proxy 每次给新 IP，一号一出口，关联度低得多。
"""
from config.env_loader import apply_env_overrides

# 总开关。关掉则退回 config/proxy.py 的 PROXY_POOL 随机抽取。
# 出问题想立刻回滚时把这个置 False，不用改代码。
REGISTER_PROXY_USE_1024 = True

# 候选国家，每次注册随机挑一个。留空则用代码里的默认列表。
# 这些都实测能连通 chatgpt.com（2026-11-17）。
# 没有 KR：实测那个出口 TLS 握手直接失败。
# 没有 EG：能连通，但埃及出口主要留给 EGP 结算，别和注册混用。
REGISTER_PROXY_REGIONS = ["US", "JP", "SG", "GB", "DE", "TW", "HK", "CA", "AU"]

# 代理保留时长（分钟）。1024proxy 上限 120。
#
# 为什么要顶满：拿到号之后还要跑 2FA 绑定和单席位探测，整条链路可能十几
# 分钟。代理中途失效后，后面每一步都会以各种莫名其妙的方式失败（卡在等
# 页面、读不到字段），很难看出根因是代理过期。多占一会儿额度换确定性。
REGISTER_PROXY_MINUTES = 120

# 取到 IP 后先验一下能不能真的访问 chatgpt.com，不通就换下一个。
#
# 必须开：1024proxy 给的 IP 不保证都能用，实测 11 个地区里有 1 个直接
# TLS 失败。不验就等于把一个坏出口直接丢给注册，白烧一个邮箱和一次接码。
REGISTER_PROXY_VERIFY = True

# 最多试几个出口才放弃。每个都要实际连一次，太大了会拖慢注册发起。
REGISTER_PROXY_MAX_ATTEMPTS = 6

# 每个国家一次取几个 IP。同一国家的多个 IP 往往同一台机器的不同端口，
# 坏起来容易一起坏，所以取 2 个就够，不够再换国家。
REGISTER_PROXY_IPS_PER_REGION = 2

# 一个出口 IP 累计产出多少个死号就拉黑它。
#
# 1024proxy 的 IP 会重复返回，而被 OpenAI 标记过的 IP 再用还是死：
# 实测 128.1.12.151 连出 3 个死号（#121/#122/#124），用它提链也被判
# unusual activity（#103 的 16 点那轮）。
#
# 默认 2：一次可能是账号自身问题或巧合，连续两次就该怀疑 IP。
# 设太高会白烧号，太低会误杀——1024proxy 可用 IP 本来就不多。
# 按主机聚合，不看端口：同一 IP 换端口对风控没有意义
#（实测注册用 24340、提链用 7004，都是 128.1.12.151）。
REGISTER_PROXY_BAD_IP_THRESHOLD = 2

apply_env_overrides(globals(), {
    'REGISTER_PROXY_USE_1024': 'bool',
    'REGISTER_PROXY_REGIONS': 'list_str_multiline',
    'REGISTER_PROXY_MINUTES': 'int',
    'REGISTER_PROXY_VERIFY': 'bool',
    'REGISTER_PROXY_MAX_ATTEMPTS': 'int',
    'REGISTER_PROXY_IPS_PER_REGION': 'int',
    'REGISTER_PROXY_BAD_IP_THRESHOLD': 'int',
})
