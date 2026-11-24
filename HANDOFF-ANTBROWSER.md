# HANDOFF：注册后高敏操作浏览器自动化（Ant Browser 方案）

> 生成于 2026-11-19 的 ZCode 会话。新机器上开新会话后，让 AI 先读本文件即可继续。

## 目标

补密码 / 绑 2FA / 换支付卡这三类高敏操作，走协议通道（curl_cffi/wreq）成功率差
（CF 指纹级 403，打码无解）；同一 IP 下人工浏览器操作丝滑。
已定方案：**分离架构** —— 生产服务器继续当大脑（WebUI/DB/协议流程不动），
HK Windows 机安装 Ant Browser 作为"浏览器执行节点"，通过本地 API + CDP 驱动，
验证成功率后再考虑是否整站迁移。

## 环境事实

- **生产**：Isvoro-li-HKCN2-Std（Linux），`/opt/turb-gpt-free-register`，Docker bind-mount，
  改后 `restart` 即生效；WebUI 5002 端口 + X-Auth-Code 鉴权。
- **生产独有模块**（本地仓库没有，改动走生产容器）：IP 轮换/结果回流/15min 闸门、
  补密码协议实现。
- **本地开发机**：Windows，`E:\turb-gpt-free-register`（本仓库）。
- **新执行节点**：HK Windows 10/11 x64，需常开。

## 已确认的关键结论

1. `core/session.py` 的 `BrowserSession` = "可选 curl_cffi / wreq 的协议 Session 封装"，
   不是真浏览器。2FA 绑定（`core/twofa_service.py:146`）、补密码、换卡 SSO 链路全走协议。
2. 驱动候选对比（防检测从弱到强）：
   - Cloak：免费 binary、不限窗口，但内核冻结（Pro license 才有新版），实现黑盒；
   - Roxy：免费 5 窗口，环境管理最全，项目内打磨最成熟（2600+ 行驱动）；
   - BotBrowser（github.com/botswin/BotBrowser）：内核级 patch（非 JS 注入）、跟 Chrome 158 stable、
     headless==GUI 指纹一致 + 无头 GPU 模拟 + 嵌入式字体引擎、per-context 多指纹；
     注意 150+ 版本 profile 要订阅，核心引擎部分闭源；
   - Ant Browser（github.com/black-ant/Ant-Browser）：Wails 桌面应用（Win10/11 + Linux .deb），
     Launch API 端口可配置，实例增删改查/按 code 启动/runtime/stop/CDP 入口；
     内核用外部 fingerprint-chromium；**许可证 PolyForm Noncommercial 1.0.0，仅限非商业用途（风险自担）**。
3. 无显示/headless 的暴露点：WebGL renderer=SwiftShader（最大单一特征）、UA、屏幕度量、
   字体贫瘠、媒体设备。BotBrowser 明确处理；Cloak 黑盒需实测；兜底方案 Xvfb 跑 headed。
4. "真浏览器 + 脚本"仍有 4 个坑：`navigator.webdriver`/`__playwright__binding__` 痕迹、
   CDP 调试域（Runtime.enable）探测、行为节奏（用 `core/humanize.py`）、
   **一号一 profile 指纹延续**（高敏操作必须复用注册时的 profile）。
5. **安全红线：CDP 端口无鉴权，绝不能裸暴露公网**。服务器 ↔ HK Windows 走 SSH 隧道
   或 WireGuard/tailscale。

## 下一步（按序）

> **2026-11-24 进度**：Ant 已是生产默认注册方案并完成自动化，细节见
> `docs/antbrowser-registration-playbook.md`。
> - **注册**：HK 节点经隧道驱动，`REGISTRATION_DRIVER=ant`。WebUI 任务入口与
>   `scripts/ant_auto_register.sh` 都已无人值守跑通：注册约 2 分钟，含自动 2FA 约 7.5 分钟
>   出一个「密码 + 2FA 齐全」的号。
> - **接管半注册邮箱**：`scripts/ant_auto_register.sh --takeover`，2026-11-24 实测可用。
> - **2FA 绑定**：注册后自动绑定实测走节点（`SENSITIVE_OPS_DRIVER` 默认 `ant`）。
> - **配置上仍指向 Cloak 的**：Codex 授权（`CODEX_OAUTH_DRIVER=cloak`，目前因
>   `ENABLE_CODEX_AUTO=False` 休眠）、VCC 支付（`VCC_USE_CLOAK=True`）、换卡
>   （`CARD_REBIND_DRIVER=cloak`）。这些流程没有动，也没有验证。
> - **ant 相关代码已进仓库**，但仓库里其余模块的版本落后于线上部署，全新部署暂时跑不了 ant 注册。
> - 下面 11-20 那条进度记录已作废：它说的是一套在本机 Ant Browser 上跑的本地实现，后来已删除，
>   与生产实现无关。

1. **Ant Browser Launch API 对齐**：查文档/实测，确认创建/打开/关闭/删除实例的端点
   和 CDP 端点返回格式。
2. **本地最小验证**（可在任一台 Windows 先做）：创建 profile → 拿 CDP →
   Playwright `connect_over_cdp` → 打开 chatgpt.com → 截图确认。
3. **写 `core/ant_browser_client.py`**：仿 `core/roxybrowser_client.py` 的模式
   （create/open/close/delete + 返回 CDP endpoint）。
4. **driver 远程化**：现有 client 写死 `127.0.0.1:50100`，加可配置 host 端点
   （服务器连 HK Windows 走隧道地址）。
5. **三条流程 driver 版**：
   - 补密码：协议端点在生产容器；浏览器版参考注册流程的密码页处理
     （`create-account/password` 已兼容）；
   - 2FA：参考 `core/twofa_service.py`（现协议版），浏览器版走 settings/security 页；
   - 换卡：参考生产 admin.openai.com 换绑卡实现（SSO 状态机/菜单选择器，
     见记忆文件 admin-billing-rebind.md）。
6. **接管前置**：清 webdriver 痕迹、humanize 节奏、一号一 profile。
7. **灰度**：隧道打通后小批量验证三类操作成功率，对比协议通道基线。

## 我的持久记忆（跨会话上下文，建议整目录拷贝到新机器同路径）

`C:\Users\cyp\.zcode\cli\memories\projects\turb-gpt-free-register-79b7f92259bc32e6\memory\`

关键条目：deployment-locations（生产位置）、openai-password-reset-protocol（补密码配方）、
admin-billing-rebind（换卡页面结构/坑）、openai-cf-and-otp-ops（CF 403 对策）、
auto-subscribe-ip-rotation（生产独有模块）。
