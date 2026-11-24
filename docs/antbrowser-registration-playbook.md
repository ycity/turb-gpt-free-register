# AntBrowser（HK 节点）注册方案 Playbook 与坑位清单

> 2026-11-23 ~ 24：Ant 注册是生产的默认注册方案（`REGISTRATION_DRIVER=ant`），并做成了脚本自动化，
> 取代原先的 Cloak 方案（`REGISTRATION_DRIVER=cloak`）。实测：注册约 2 分钟，注册后冷却
> 180s 自动绑 2FA，约 7.5 分钟得到一个「密码 + 2FA 齐全」的池账号。
> - 经 WebUI 任务接口、经 `scripts/ant_auto_register.sh` 各有一轮全程无人干预的验收。
> - 半注册邮箱的接管（`--takeover`）2026-11-24 实测可用。
>
> 注册过程中验证码到不到、走哪条页面路由都和出口有关，排查 OTP 问题先看出口，别急着归因平台。
>
> 本文不写具体的出口地址、账号、服务器地址等运营信息——仓库是公开的，这类信息放在部署环境的
> `.env` 和任务日志里。

## 代码在哪

| 文件 | 作用 |
| --- | --- |
| `core/ant_browser_client.py` | 节点 Launch API 客户端（建/启/停/删实例，取 CDP 地址） |
| `core/ant_node_bridge.py` | 真实键鼠栈、节点串行锁 `_NODE_FLOW_LOCK`、`_ant_page` 上下文、登录流程、CF 质询处理 |
| `core/ant_registration_bridge.py` | `ant_register_flow`（页面流程，含接管模式）+ 适配层 `run_ant_registration`（领邮箱、抽代理、入库、失败处置） |
| `core/ant_auto_register.py` | 自动化：预检、提交、等待、验收、汇总，以及接管半注册邮箱（容器内运行） |
| `scripts/ant_auto_register.sh` | 宿主机入口（`scripts/` 不在容器挂载里） |
| `config/antbrowser.py` | `ANT_*` 配置 |
| `core/auto_register_scheduler.py`、`config/auto_register.py` | 应用内自动注册调度器（脚本复用它的邮箱余量统计） |
| `tests/test_ant_registration.py` | 流程分支 / 适配层 / 自动化 / 节点登录流程的单元测试（72 个，全 mock，不碰库、网络、节点） |

调用链：`main.py:run_registration`（`REGISTRATION_DRIVER=ant`）→ `run_ant_registration`
→ `ant_register_flow` →（HK 节点上的 Ant Browser 实例）→ `save_account_data` 入库
→ 标准 `registration_auto` 钩子排 2FA。

**注意**：ant 驱动在运行时会用到 `core.db`、`core.account_export` 等模块里较新的接口。仓库里这些
模块的版本落后于线上部署的版本，所以单凭 ant 相关文件，从仓库全新部署暂时跑不起 ant 注册；
要等其余模块同步进仓库之后才行。

## 三种发起方式（同一条注册路径）

1. **脚本（按需 / 批量 / 可 cron）**
   ```bash
   ./scripts/ant_auto_register.sh                 # 注册 1 个
   ./scripts/ant_auto_register.sh 3               # 注册 3 个，连续失败 2 次即停
   ./scripts/ant_auto_register.sh 5 2 --gap 0     # 两号之间不额外等待
   ./scripts/ant_auto_register.sh --preflight     # 只预检，不提交任务
   nohup ./scripts/ant_auto_register.sh 5 > /dev/null 2>&1 &     # 后台跑，日志在 logs/ant-auto-register/
   ```
   - 退出码：0 全部成功 / 1 有失败或提前停止 / 2 预检未通过（一个任务都没提交）/ 3 容器未运行
   - 每个号结束输出一行 `RESULT {...}`（JSON），便于外部解析
   - 两号间隔默认取 `AUTO_REGISTER_INTERVAL`，从上一个号 2FA 结束算起，`--gap` 可覆盖
   - Ctrl-C 只中断脚本，已提交的注册任务在主进程里继续跑完
2. **WebUI 任务页「开始注册」**。ant 下任务在节点上串行执行，并发数开大了也只是排队。
3. **应用内自动注册调度器**（`core/auto_register_scheduler.py`，WebUI 开关）。默认关闭；
   开启后按 `AUTO_REGISTER_INTERVAL` 持续从邮箱池注册，邮箱池空了就停。它会和脚本互相等待，不会撞车。

## 接管半注册邮箱

注册在「密码已提交」之后失败，OpenAI 侧其实已经建了号，只是邮箱验证或资料页没走完。这类邮箱在池里
标 `failed`、密码写在备注里，可以用 `--takeover` 接回来：从登录页用已有密码登录（没有密码记录的用
一次性验证码登录），补完验证和资料后入库，再等自动 2FA。

```bash
./scripts/ant_auto_register.sh --takeover --list                 # 列出可接管的半注册邮箱
./scripts/ant_auto_register.sh --takeover --all                  # 全部接管
./scripts/ant_auto_register.sh --takeover --email user@example.com
```

- 只接管清单里的邮箱（状态是 `failed` 且备注是半注册）；指定了不在清单里的邮箱会被跳过，不会被拿去登录。
- 接管在本脚本进程里直接跑页面流程，和注册不一样，没有对应的 WebUI 任务类型。

## 走通的最短路径

```
预检         驱动=ant、节点在线且空闲、无未结束任务/2FA、邮箱池有余量、有节点可达出口、WebUI 可用
提交         POST /api/jobs {count:1, workers:1} → 主进程执行
页面流程     chatgpt.com/auth/login → 邮箱页 → 提交
             → email-verification → 点「使用密码继续」       （或直达下一步，见坑 4）
             → /create-account/password → 设密码 → Continue
             → email-verification → 创建账户确认验证码（取码敲入，可多轮）
             → /about-you → 姓名 + 年龄 → 继续               （可能弹确认框，见坑 5）
             → chatgpt.com → fetch /api/auth/session → accessToken
入库         save_account_data：密码、session.user/account、codex=skipped
收尾         180s 冷却 → 自动绑 2FA（走节点登录 + 协议 setup_2fa）→ 单席位探测
```

实测耗时：注册 2 分钟左右（验证码在好出口上 9–10 秒到达），冷却 3 分钟，2FA 约 2 分钟。

## 失败时邮箱去向

| 情形 | 邮箱状态 | 说明 |
| --- | --- | --- |
| 注册成功 | `used` | 账号入库，密码 + 2FA |
| 密码提交**之前**失败（入口打不开、页面不到密码页等） | `available` | 退回池子，可再领 |
| 密码提交**之后**失败（验证码/资料页/取令牌失败） | `failed` | OpenAI 侧已建号，**密码写在池备注里**，可用 `--takeover` 接管；绝不退回池子 |
| 邮箱在 OpenAI 已有账号（直达登录密码页） | `disabled` | 无法再注册 |
| 撞上未知的 2FA 码页 | `disabled` | 无法接管，废弃 |

服务层后处理只回收「仍为 used」的邮箱，不会覆盖上面这些状态。

## 代理与出口

- 代理池 `PROXY_POOL` 是**多流程共用**的：本机协议流程（查活/套餐/席位预热/补密码）和 HK 节点的浏览器流程都用它。
- 同一条代理可能「从本机通、从 HK 节点 TCP 不通」，反之亦然。`ANT_PROXY_EXCLUDE` 列出节点不可达的出口，
  ant 注册抽代理时跳过，其它流程照常使用。
- **验证码被压制是按出口来的**。16 条节点可达的出口里，经节点注册验证过验证码秒到的只有少数几条，
  其余尚未在注册中走到验证码这一步，是否被压制未知。
- 每单实际用的出口记在任务日志里：`[Ant注册] <邮箱> 本轮代理出口：<host:port>`。

## 排障

```bash
# 预检（只读）
./scripts/ant_auto_register.sh --preflight
# 节点是否在线 / 被占用（地址取部署环境里的 ANT_API_BASE）
curl -s "$ANT_API_BASE/api/health" ; curl -s "$ANT_API_BASE/api/runtime/active"
# 某单卡在哪：看任务日志（注册日志/<job_uuid>.log）与 2FA 日志（注册日志/twofa-<邮箱>.log）
# 尾段循环不打日志，卡住时用现成的只读探针看页面最直接（容器内；节点空闲时输出 NO_ACTIVE_INSTANCE）：
docker exec -w /app -e PYTHONPATH=/app turb-gpt-free-register python core/ant_cdp_tool.py pages          # 各页面 URL
docker exec -w /app -e PYTHONPATH=/app turb-gpt-free-register python core/ant_cdp_tool.py text 1          # 第 1 个页面的可见文字
docker exec -w /app -e PYTHONPATH=/app turb-gpt-free-register python core/ant_cdp_tool.py shot 1 <名字>   # 截图
# 单元测试（全 mock，不碰库/网络/节点）
python tests/test_ant_registration.py
```

OTP 超时按顺序查：本单出口 → 该出口是否真生效（页面内读出口 IP）→ 收信含 Junk 是否正常 → 换一条已验证的好出口对照。

## 坑位清单

**坑1：HK 节点 AntBrowser 是桌面应用，没人登录桌面 = 节点不可用**
- 隧道服务是 active，但 `/api/health` 无响应。节点上的 SSH 账号是受限账号，远程拉不起来，要人工登录桌面并启动。
- 脚本预检会直接拦住，不会提交任务。

**坑2：代理池是共用的，「从本机不通」不等于「从节点不通」，反之亦然**
- 曾凭一次探测超时把一条住宅出口当死代理从池里删了。实际它活得好好的，只是 HK 节点到它 TCP 不通（两次复现，同批对照的两条都通）。
- 正确做法：留在池里 + 用 `ANT_PROXY_EXCLUDE` 只让 ant 跳过。改共用配置前先复核、先问。

**坑3：注册驱动设成 ant 后，`CODEX_OAUTH_DRIVER=same_as_registration` 是死路**
- Codex 授权驱动跟随注册驱动得到 `ant`，而 ant 没有独立的 Codex 授权驱动，强制授权会报「不支持的 CODEX_OAUTH_DRIVER='ant'」。
  `ENABLE_CODEX_AUTO=False` 时是潜伏问题，手动点一次授权才炸。
- 现状：`.env` 里显式写 `CODEX_OAUTH_DRIVER="cloak"`，行为与切换前一致。

**坑4：提交邮箱后可能不经验证页，直接落到创建密码页**
- 流程原先只认「点到使用密码继续」，结果干等 420s 超时，把一步之遥的注册判成失败。
- 已修：分岔循环把 `/create-account/password` 也当作可以往下走。

**坑5：年龄偏小时，资料页点「继续」会弹「你正在将出生日期设置为…」确认框**
- 要点「确定」才继续；年龄大的会话不弹。
- 已修：资料页循环里用整词匹配 `^(确定|确认|confirm)$` 点确认，不会误点「继续」「取消」。

**坑6：密码已提交后失败，邮箱不能退回池子**
- 退回去会被再领到，撞「已有账号」后被停用，等于白丢一个已建好的号。
- 已修：流程函数用 `progress["password_submitted"]` 标记进度，适配层据此把邮箱标 `failed` 并把密码写进备注。

**坑7：WebUI 驱动下拉原先没有 ant**
- 值是 `ant` 时不会保存动作改掉，但界面只显示生硬的 `ant`，且一旦切到别的驱动就选不回来。已补选项和帮助文案
  （`index.html` 是 CRLF，原地补丁要保留换行符）。

**坑8：取信只扫 INBOX**
- 新池邮箱的来信会被 Outlook 分进 Junk。`outlook_client` 三条取信通道都已合并 Junk；在那之前的「OTP 没到」要考虑是不是漏读了 Junk。

**坑9：节点是单窗口，且有别的流程在用**
- 统一 CDP 入口同一时刻只有一个活动实例。2FA 绑定、订阅等流程都走它。脚本每轮发起前会等「节点空闲 + 无未结束任务 + 无进行中 2FA」。

**坑10：登录入口别直开 `auth.openai.com/log-in`**
- 接管首次实测：直开登录页落到「会话已失效（`invalid_state`）」，那页的「重试」绕回来的正是注册用的 `chatgpt.com/auth/login`。
- 已修：注册和接管共用同一个入口；入口循环认得「会话已失效 → 重试」，最多两次，反复失效就明确报错。

**坑11：节点登录流程（2FA 绑定、补密码共用）的三个缺口**
- 库里没有密码的账号也会去点「使用密码继续」，落到密码页后卡死。账号在 OpenAI 侧其实可能有密码（只是没记下来），这个入口照样会出现。
- 密码登录通过后又要求验一次邮箱（换出口/设备时常见），流程不处理，干等到超时。
- 密码路径下 `otp_round` 未定义，按 cookie 判定登录成功时会 `NameError` 并被兜底吞掉，页面不在 chatgpt.com 时会误判成登录失败。
- 均已修，并有回归测试。

**坑12：终局异常曾被尾段循环的兜底吞掉**
- 流程尾段有一个「什么异常都接住、只打 debug 日志」的兜底。「撞上未知 2FA 就废弃邮箱」「第二轮验证码没取到」
  这类必须冒泡的信号以前也被它吞成干等到超时。现已让这几类异常直接抛出。

**坑13：运维小坑**
- 重启容器用 `scripts/safe-restart.sh --wait N`，别直接 `docker restart`（会打断 2FA/注册/支付）。
- `.env` 的 `PROXY_POOL` 是单行引号串，条目分隔是**字面量 `\n`**，还混着 `# 注释行`；远程 heredoc 改它翻过车，一律写脚本文件上传执行。
- `scripts/`、`tests/` 不在容器挂载里。
- `pgrep -f 'xxx'` 会匹配到包含该串的远程命令自己，用 `pgrep -f 'xx[x]'`。后台进程要把整个子 shell 的输出都重定向，否则 SSH 会话不返回。

## 未决事项

- **本次只替换了注册。** 配置上仍指向 CloakBrowser 的流程：Codex 授权（目前因 `ENABLE_CODEX_AUTO=False` 休眠）、
  VCC 支付、换卡。这些流程没有动，也没有验证。
- **接管时没有密码记录的账号**：入库后没有密码，只能用验证码 / TOTP 登录，要另行走补密码。
- **多数出口未在注册中验证过**：它们是否压制验证码未知，跑到失败时日志里有出口可查。
- **仓库其余模块落后于线上版本**（见「代码在哪」）。
