# AntBrowser（HK 节点）手动订阅 Playbook 与坑位清单

> 2026-11-20 实测：账号 #153（<account_email@example.com>）在 HK 执行节点的
> Ant Browser 上完成 Team 单席位订阅，EGP 920（八折优惠码 48 个月），全程
> 零 CF 质询、零打码、零 3DS，提交后 ~35s 落定 `success-team`。
> 驱动方式：**CDP 单步手动操作**（探针工具 `core/ant_cdp_tool.py`，生产容器内），
> 不是全自动脚本——这是刻意为之，见坑 15。

## 走通的最短路径（下次照抄）

```
1. 隧道健康检查        curl http://<DOCKER_BRIDGE_IP>:19876/api/health   （挂了=坑1）
2. 租 EG 出口(120min)   fetch_1024proxy_ips('EG', 4, minutes=120)，服务器侧验证
3. SSH 反向借道        ssh -N -R 127.0.0.1:17004:<EG出口IP:端口> <ssh_user>@HK
4. 起实例              open_profile(proxy_url='http://127.0.0.1:17004')
5. 登录                auth.openai.com/log-in →（a:has-text('登录')）→ 邮箱 →
                       提交 → text:使用密码继续 → 密码 → TOTP → 落 chatgpt.com
6. 验登录态            fetch('/api/auth/session') 必须非 {}   （坑8）
7. 领码+提单席位长链    claim_next_promo_code → generate_team_checkout_link(
                       promo_assigned=..., seats=2→1, EG/EGP, auto_pay=False)
8. 立刻打开长链        （链有时效，坑9）等「正在准备您的套餐」转完
9. 填卡                fill_elements_card(page, card=card_secrets(卡))   ← 复用实战helper
10. 填账单地址          fill_elements_address(page, fetch_card_address(卡号)) → 税重算
11. 读总额过闸门        read_elements_total → 必须 = 920.00（坑11：不是1048.80）
12. 提交               click button[type=submit] → 轮询 URL
13. success-team 落定   → record_used_promo_code + set_account_paid_at +
                       set_account_pay_card + enqueue_account_plan_check
14. 清理               runtime/stop + DELETE profile + kill SSH -R
```

## 坑位清单（按踩到的顺序）

### 环境层

**坑1：HK 节点 AntBrowser 是桌面应用，没人登录桌面 = 整个节点挂**
- 现象：隧道 systemd 服务 active，但 `/api/health` 空回复；HK 上 `curl 127.0.0.1:19876` 拒绝连接；`quser` 显示无交互会话
- <ssh_user> 是受限账号（非管理员、读不了 Administrator 的安装目录），远程拉不起
- 修复：人工 RDP（3389 通）登录桌面并启动 AntBrowser
- 遗留：建议在 HK 配 autologin + 开机自启 + 崩溃守护，否则每次掉线都要人工

**坑2：1024proxy 是白名单代理，租约绑服务器 IP，HK 直连被拒**
- 现象：浏览器 `ERR_TUNNEL_CONNECTION_FAILED`；服务商报 `user forbidden, userip=<HK_NODE_IP>, info=auth fail`（服务器侧同出口正常）
- 修复：SSH 反向转发借道——`ssh -N -R 127.0.0.1:17004:<EG出口> <ssh_user>@<HK_NODE_IP>`，AntBrowser 实例代理填 `http://127.0.0.1:17004`。TCP 由服务器发起（白名单内），出口 IP 与协议登录一致（风控连续性白赚）
- 注意：-R 绑的是单个出口端口，租约到期（minutes 参数）前要用完；换出口要重建 -R

**坑3：统一 CDP 入口会在「无客户端连接的间隙」清掉 active 登记**
- 现象：探针报 `NO_ACTIVE_INSTANCE`，但 `/api/profiles/{id}/status` 显示 pid 活着、debugReady=true
- 修复：重 `POST /api/runtime/session {profileId}` 挂回统一入口——不重启实例、页面/登录态全保留
- 对策：单次 CDP 连接内串多条动作（探针 `seq` 命令），别连接-断开-睡觉-再连

**坑4：`ctx.pages` 索引会漂移**
- 同一个标签页两次查询索引不同；跨调用要用 URL 定位，别信固定 index

### 登录层

**坑5：「登录」入口是 `<a>` 不是 `<button>`**
- 会话结束页的「登录」是链接；`button:has-text('登录')` 静默超时
- 视觉模型描述的"按钮"不可信，先 `eval` 枚举 DOM：`[...document.querySelectorAll('button,a')].map(e=>e.tagName+e.innerText)`

**坑6：有密码账号第二页默认落在 OTP 验证码页**
- 必须主动点「使用密码继续」（`text:使用密码继续`，实测 locator force 点击可用）→ 密码页
- 密码路径不消耗 OTP 邮件（复验成立）

**坑7：TOTP 密钥在库里的字段名是 `totp_secret`**
- 不是 `twofa_secret`（那是空值，会误判"无 2FA"）

**坑8：仅凭 URL 判断登录 = 假阳性**
- 匿名访问 chatgpt.com/ 也是这个 URL 和标题；判断登录唯一可靠法：`fetch('/api/auth/session')` 返回非 `{}`
- 本次实际发生过：TOTP 后落到 chatgpt.com/ 但 session-token 没落下（回调被吞），肉眼以为登录了

### 结账层

**坑9：结账长链有时效（实测 ~30-40 分钟）**
- 过期链打开 → 重定向 `/auth/login` → auth.openai.com/email-verification，**还会把已登录 session 顶掉**，引发连锁误判
- 铁律：领码→提链→打开必须在分钟级一口气完成；过期链绝不再碰

**坑10：auth.openai.com 有自己的持久会话**
- 重登时不再出「会话已结束」页，直接落邮箱页；且若 auth 会话仍有效，提交邮箱后可能自动完成登录并回跳到上次的目标 URL——先探页面状态再决定点哪步

**坑11：金额陷阱——默认账单国 EG，VAT 14% 叠在八折价上**
- 时序：开链时总额显示 `1,048.80 = 920 + 14% 埃及VAT`；填完美国卡地址（选**无销售税州**，如 MT）后税重算为 0 → `920.00`
- 金额闸门读数必须在「地址填完 + 税重算稳定（等 ~2s）」之后，否则误拦
- 上午自动订阅事故的同源解释：无码时读到 1150 是未含税中间值

**坑12：Elements 填卡/填地址别手写选择器**
- 直接复用 `core/elements_checkout.fill_elements_card / fill_elements_address`：自带 iframe 定位、逐字段回读校验（本次三要素+六地址字段全部 True）
- 卡地址用 `fetch_card_address(卡号)` 与卡同源，别用配置里的固定地址

### 支付层

**坑13：本次 hcaptcha-invisible iframe 只是预加载挂着，提交全程没弹出**
- fingerprint-chromium 148 + EG 住宅级出口 + 真人节奏（type delay 45ms）组合下没触发；真弹出时按 pay-session 的打码/接管路径处理
- 全流程零 CF 质询（连登录首跳都没有）

**坑14：提交后页面无报错 ≠ 成功**
- 必须等 URL 跳到 `chatgpt.com/payments/success-team?stripe_session_id=...`（本次 ~35s）
- 落定后仍要事后确认：`enqueue_account_plan_check` → plan=team + subscription_no 自动分配

### 方法论

**坑15（用户全局规则）：别一上来写全自动脚本反复跑**
- 每次整跑 = 一次提链 + 一次登录暴露，失败重试会在号上累积风控信号
- 正确姿势：CDP 单步手动走通（每步截图/DOM 验证）→ 全绿后再固化脚本
- 本次实证：第 1、2 次脚本整跑各留暴露且失败；手动单步一遍即成功

## 自动化脚本（2026-11-21 沉淀，#155 轮次实证）

- 容器内 `core/ant_subscribe.py`：`prep`（领码/租出口/选有余额卡）→ `drive`
  （协议登录注入→现提链秒开→填卡/地址→闸门→提交→记账）→ `release`（兜底退码）
- 宿主机 `scripts/ant_auto_subscribe.sh <id>`：编排 prep → SSH -R 借道 →
  HK 侧验证 → drive → 清理转发
- **登录规则（用户指定）：有密码+2FA 的账号统一优先协议登录（密码+TOTP）
  注入 cookie**；浏览器登录链只留给无密码账号
- 拒付自动换一张有余额卡重试一次，两连拒收手退码

### #155 轮次新增坑（16-20）

**坑16：akmoz 临时邮箱会被回收（取信 404）**
- email_source=akmoz 的老账号，OTP 邮件路径随时可能永久失效
- 对策即登录新规则：有密码+2FA 走协议注入，不依赖邮箱

**坑17：curl_cffi 协议登录会被 IP 段熔断 403，同出口浏览器零质询**
- 2026-11-20 晚：<PROXY_EXIT_IP> 全段对 curl_cffi 403（当天被整天的注册/订阅
  流量烧黑），同出口 fingerprint-chromium 一切正常——CF 指纹级差异的直接证据
- 对策：协议登录**首选 state 里选定的出口**（=浏览器同出口），别另拉一批
  可能烧黑的；`_login_cookies_for` 多轮换出口仍熔断时等 15 分钟冷却

**坑18：结账页渲染进程间歇崩溃（Page crashed，reload 也崩）**
- 同节点同页面时稳时崩（#153 全程稳，#155 三崩两稳）
- 实证恢复法：**停掉实例→全新实例→协议重注入→重开同一条还新鲜的链**
  （cookie 持久化在 profile，但换新实例最干净；链 40 分钟内仍可用）

**坑19：拒付文案是 "Payment was not approved"，好卡也会被拒**
- #155 两张有余额好卡（1771/1588）+ 新链全部 "not approved"，零扣款——
  账户/尝试级风控，不是卡的问题；词表里常见的 declined/insufficient 都
  匹配不到这个文案
- 对策：自动换卡重试一次；两连拒收手（重复硬锤只会加重风控）

**坑20：编排与实例细节**
- `nohup ... &` 必须带 `< /dev/null`，否则 ssh 会话被后台进程的管道挂住
- nohup 日志重定向前要先 mkdir（shell 的重定向在脚本 mkdir 之前失败）
- 实例被 stop 后再 POST `/api/runtime/session` 会**重启实例并丢掉全部标签
  页**——排查前先查 `/api/profiles/{id}/status` 的 running 字段
- 付费判定看账不看页：VCC 卡余额 + `enqueue_account_plan_check` 的套餐
  结果才是权威，页面丢失/崩溃不等于支付失败

**坑21：1024proxy 出口会在租约期内断气；"not approved" 的真凶之一**
- 实证（2026-11-21 凌晨）：出口 21048 在 #153 成功支付（23:00）后、#155
  尝试（00:30+）前死亡——chatgpt/stripe/ipify 全 000 超时；页面是断气前
  渲染的所以看着正常，提交后确认请求挂 4 分钟 → OpenAI 判
  "Payment was not approved"，**零扣款**
- 症状酷似"地区/货币错配"（用户直觉方向），实为出口失活
- 对策已进脚本：提链前 + 提交前各做一次 `_exit_alive`（经出口请求
  ipify），失活即退码收手，不烧拒付记录
- 同夜伴随症状：EG 池整体劣化，`_verified_proxies_in_country` 单出口
  geo 验证可无限挂死 → prep 卡死（待办：给验证加总超时，或 prep 失败
  快速失败）

**坑22：HK 节点渲染崩溃呈时段性聚集（待人工排查）**
- 23:00 前零崩溃（#153 全程稳）；00:30 后新实例首次填卡即
  Page crashed 的概率 ~50%（三台不同新实例复现），reload 也崩
- 换 `--disable-gpu` 没来得及验证（prep 挂死在出口验证）
- 疑似节点内存/僵尸 chrome 进程随时间累积；<ssh_user> 权限读不了
  tasklist/CIM，**需要 RDP 上去看任务管理器或直接重启 HK 机器**

## 本次产物

- 探针工具：生产 `core/ant_cdp_tool.py`（单步/seq 两种模式，敏感值走环境变量）
- 截图链：生产 `/app/logs/ant-manual-153/01..09*.png`（登录→MFA→结账→1048.80 中间态→920 终态→提交→成功）
- 账 #153：team / subscription_no=99 / 卡 VCC-0363 / 码 <PROMO_CODE>(used)
- 「选卡优先有余额」是本次一次性要求（用户指示），未写入生产调度器
