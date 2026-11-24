# -*- coding: utf-8 -*-
"""Ant Browser 注册流（模仿用户真实点击）。

页面序列（2026-11-23 经 WebUI 任务入口实测，#216/#217）：
  chatgpt.com/auth/login → 邮箱页(真实键盘) → 提交
  → email-verification：真点击「使用密码继续/Continue with password」
    （个别会话提交邮箱后不经此页、直达 /create-account/password，同样放行）
  → /create-account/password（新邮箱=注册分支）→ 真实键盘设密码 → Continue
  → email-verification：创建账户确认验证码（取码 → 真实键盘敲入）
  → /about-you（Full name + Age）→ 真实键盘 → Continue
    （年龄偏小时会弹「你正在将出生日期设置为…」确认框 → 再点「确定」）
  → chatgpt.com 登录态 → 页面内 fetch /api/auth/session → accessToken

设密码后仍需要一封「创建账户确认」验证码；它到不到取决于出口信誉
（好的出口 10 秒内到达，被压制的出口永远不来），所以驱动只从节点可达的出口里选。
输入用 core.ant_node_bridge 的真实键鼠栈（_human_type/_human_submit/
_force_click_by_text，2026-11-20 在登录链 6/6 验证）。
"""
from __future__ import annotations

import logging
import random
import time

from core.ant_node_bridge import (
    _ant_page, _human_type, _human_submit, _force_click_by_text, _click_entry_button,
    _detect_challenge, _wait_out_challenge, _heal_chrome_error,
    _export_cookies, _EMAIL_INPUT_SELECTORS, _EMAIL_SUBMIT_SELECTORS, _OTP_INPUT_SELECTORS,
)

logger = logging.getLogger(__name__)


class EmailAlreadyRegistered(RuntimeError):
    """池邮箱在 OpenAI 侧已有账号（提交后直达 /log-in/password）。"""


class EmailHasUnknown2FA(RuntimeError):
    """OpenAI 侧已有我们不知道的 2FA（MFA 码页），账号无法接管，邮箱应废弃。"""


class RegistrationAdvanced(RuntimeError):
    """注册已过密码页但未完成（半注册）：OpenAI 侧已有账号+密码，邮箱不能回
    available 再注册，应标 failed 留给补密码流程接管。password 属性带本轮密码。"""

    def __init__(self, message: str, password: str = ""):
        super().__init__(message)
        self.password = str(password or "")


class OtpRoundFailed(RuntimeError):
    """验证码第 2 轮及以后没拿到新码：再等下去没有意义，直接失败（不能被尾段兜底吞掉）。"""


class TakeoverLoginFailed(RuntimeError):
    """接管半注册账号失败（密码未被接受、登录后没走完等）。账号本来就在 OpenAI 侧，
    邮箱池状态不需要变动，调用方如实报告即可。"""


_SIGNUP_TIMEOUT = 420  # 全链超时（秒）

# 「使用密码继续」的多语言文案（Cloak 流实测 ja/en/zh 三态）
_PW_LINK_PATTERNS = [r"password|密码で続行|使用密码继续|continue with password"]
# about-you 页
_NAME_INPUT = ["input[name='name']", "input[autocomplete='name']"]
_AGE_INPUT = ["input[name='age']", "input[type='number']"]

# 未知 2FA（MFA/authenticator 码页）识别：URL 关键词优先；
# email-verification/about-you 是正常流程页，绝不能误判成 2FA。
_MFA_URL_PATTERNS = ("mfa", "authenticator", "two-factor", "2fa", "otp-challenge")


def _looks_like_mfa_page(page) -> bool:
    url = str(page.url or "").lower()
    if "email-verification" in url or "about-you" in url:
        return False
    if any(p in url for p in _MFA_URL_PATTERNS):
        return True
    try:
        text = str(page.evaluate(
            "(document.body.innerText||'').slice(0,400)") or "").lower()
    except Exception:
        return False
    return any(k in text for k in
               ("authenticator", "two-factor", "两步验证", "身份验证器", "验证器应用"))


def _visible_css(page, css_list):
    for css in css_list:
        try:
            if page.evaluate(
                "(css)=>{const v=el=>!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);"
                "return [...document.querySelectorAll(css)].some(v)}", css):
                return css
        except Exception:
            continue
    return None




def _handle_create_otp(page, email: str, after_ts: float, *, max_wait: int = 240,
                       attempts: int = 2, skew: int = 120, label: str = "创建账户") -> bool:
    """验证码页：取码→真实键盘敲入→提交。页面上没有验证码框时返回 False。

    skew：邮件时间比 after_ts 早多少秒以内也算数（容忍 OpenAI 邮件时间戳与本机时钟的偏差）。
    第 1 轮沿用 120s；后续轮次必须收紧，否则会把上一轮用过的码再取一遍。
    """
    css = _visible_css(page, ["input[name='code']", "input[autocomplete='one-time-code']"])
    if not css:
        return False
    logger.info("[Ant注册] %s 需要%s邮箱验证码，取码中...", email, label)
    from core.email_provider import wait_for_otp

    code = ""
    for _fr in range(attempts):
        try:
            code = wait_for_otp(email, after_ts=after_ts - skew, max_wait=max_wait, force_service=True)
        except Exception as exc:
            logger.warning("[Ant注册] 取码失败重试：%s", str(exc)[:100])
            time.sleep(15)
        if code:
            break
    if not code:
        raise RuntimeError(f"注册：未取到{label}验证码")
    if not _human_type(page, css, code):
        raise RuntimeError(f"注册：{label}验证码敲入失败")
    time.sleep(0.8)
    _human_submit(page, _EMAIL_SUBMIT_SELECTORS)
    time.sleep(5)
    return True


def _feed_otp_round(page, email: str, otp_state: dict) -> bool:
    """验证码页要码就喂一轮，返回这一轮是否已提交。

    同一次登录里 OpenAI 可能连要两轮验证码（接管半注册号时：登录验证 + 创建账户确认；
    或新风控下提交第一轮后立刻再发一封）。判定沿用登录链验证过的规则：上一轮提交
    8 秒后验证码框还在，就算新一轮。第 2 轮起只认上一轮提交之后到达的邮件，
    等不到就抛 OtpRoundFailed——不能让尾段循环兜底把它吞成干等。
    """
    if not _visible_css(page, _OTP_INPUT_SELECTORS):
        return False
    done = int(otp_state.get("round") or 0)
    if done >= 4:
        raise OtpRoundFailed("验证码已提交 4 轮仍未通过")
    if done >= 1 and time.time() - otp_state["last_submit"] < 8:
        return False            # 上一轮刚提交，页面还在处理
    n = done + 1
    if n == 1:
        ok = _handle_create_otp(page, email, otp_state["after_ts"])
    else:
        logger.info("[Ant注册] %s 出现第 %s 轮邮箱验证，等新的验证码", email, n)
        try:
            ok = _handle_create_otp(page, email, otp_state["last_submit"], max_wait=120,
                                    attempts=1, skew=3, label=f"第 {n} 轮")
        except RuntimeError as exc:
            raise OtpRoundFailed(str(exc)) from exc
    if not ok:
        return False
    # 只有这一轮真的提交了才推进状态；没取到码时保持原状，下一轮循环重试的是同一轮
    otp_state["round"] = n
    otp_state["last_submit"] = time.time()
    return True


def _submit_password(page, email: str, password: str, deadline: float, *, takeover: str = "") -> None:
    """密码页：等输入框就绪 → 真实键盘敲入（含确认框）→ 提交。

    注册模式下落到 /log-in/password 说明邮箱在 OpenAI 侧已有账号，交给调用方换邮箱；
    接管模式本来就是要登录已有账号，放行。
    """
    pwd_css = None
    while time.time() < deadline:
        if _detect_challenge(page).get("active"):
            _wait_out_challenge(page, timeout=60.0, step="reg_pwd_wait")
            continue
        url = str(page.url or "")
        if "/log-in/password" in url and not takeover:
            raise EmailAlreadyRegistered(
                f"{email} 已有 OpenAI 账号（使用密码继续 → /log-in/password），换邮箱")
        if "/create-account/password" in url or (takeover and "/log-in/password" in url):
            pwd_css = _visible_css(page, ["input[type='password']"])
            if pwd_css:
                break
        time.sleep(1.5)
    if not pwd_css:
        raise RuntimeError(f"注册：密码页未就绪 url={page.url[:80]}")
    if not _human_type(page, pwd_css, password):
        raise RuntimeError("注册：密码敲入失败")
    time.sleep(0.5)
    # 确认框（第二个空密码框）
    try:
        others = page.evaluate("""() => {
          const v = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          return [...document.querySelectorAll("input[type='password']")].filter(v)
            .filter(el => el.value === '').length;
        }""")
    except Exception:
        others = 0
    if others:
        _human_type(page, "input[type='password']", password)
    time.sleep(0.8)
    _human_submit(page, _EMAIL_SUBMIT_SELECTORS)
    logger.info("[Ant注册] %s 密码已提交", email)


def _codex_step(email: str, proxy: str) -> dict:
    """注册后的 Codex 授权：与 cloak/roxy 驱动同一套开关（ENABLE_CODEX_AUTO）。

    关着时记 skipped；开着时按 CODEX_OAUTH_DRIVER 指定的驱动跑（ant 节点没有独立的
    Codex 授权驱动，该配置不要设成 same_as_registration）。任何异常都转成 failed 结果，不向上抛。
    """
    result = {
        "status": "skipped",
        "ok": True,
        "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
    }
    try:
        from config import codex as _codex_cfg
        if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
            from core.codex_oauth import run_codex_oauth
            logger.info("[Ant注册][Codex] ENABLE_CODEX_AUTO=True，注册后执行 Codex 授权")
            result = run_codex_oauth(email, proxy=proxy, force=True)
        else:
            logger.info("[Ant注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
    except Exception as exc:
        result = {"status": "failed", "ok": False,
                  "message": f"{type(exc).__name__}: {str(exc)[:180]}"}
    return result


# about-you 页点「继续」后，年龄偏小时 OpenAI 会弹「你正在将出生日期设置为…」确认框，
# 要再点一次「确定」才会继续（2026-11-23 实测，年龄 21；年龄大的会话不弹）。
# 整词匹配，避免误点同页的「继续」「取消」。
_BIRTHDATE_CONFIRM_PATTERNS = [r"^(确定|确认|confirm)$"]


def _confirm_birthdate_dialog(page, email: str) -> bool:
    """资料页若停着出生日期确认框就点「确定」，返回是否点到了。"""
    hit = _force_click_by_text(page, _BIRTHDATE_CONFIRM_PATTERNS)
    if hit:
        logger.info("[Ant注册] %s 已确认出生日期弹窗(%s)", email, hit)
    return bool(hit)


# 登录会话失效落地页：「会话已失效 / 您的登录会话已失效。请重新开始以继续」+「重试」按钮
# （诊断信息里是 error_code: invalid_state）。2026-11-24 实测：点「重试」回到
# chatgpt.com 的登录表单，可以接着走。只认整词的「重试」，且页面文字得确实是会话失效。
_SESSION_EXPIRED_MARKS = ("会话已失效", "会话已结束", "session has expired", "session expired", "invalid_state")
_SESSION_RETRY_PATTERNS = [r"^(重试|再试一次|重新开始|retry|try again|start over)$"]


def _session_expired_page(page) -> bool:
    try:
        text = str(page.evaluate("(document.body.innerText||'').slice(0,600)") or "").lower()
    except Exception:
        return False
    return any(mark.lower() in text for mark in _SESSION_EXPIRED_MARKS)


def ant_register_flow(email: str, *, proxy: str, password: str = "",
                      full_name: str | None = None, age: int | None = None,
                      progress: dict | None = None, takeover: str = "") -> dict:
    """Ant 浏览器注册一个新账号，或接管一个半注册账号。成功返回
    {access_token, user_id, user_name, expires, user, account, cookies, profile_id}。

    progress（可选）：调用方传入的 dict。密码提交成功后置
    progress["password_submitted"]=True，供调用方在失败时判断 OpenAI 侧是否
    已经建号——已建号的邮箱不能退回池子，要按半注册隔离并保住密码。

    takeover：接管 OpenAI 侧已建号、但邮箱验证/资料页没走完的账号（邮箱池里标 failed
    的半注册）。"password" = 从登录页用已有密码登录后补完；"otp" = 没有密码，用一次性
    验证码登录后补完。两种都复用注册的后半程（邮箱验证码 → 资料页 → 取令牌），
    接管模式不动 progress。失败抛 TakeoverLoginFailed。
    """
    takeover = str(takeover or "").strip().lower()
    if takeover not in ("", "password", "otp"):
        raise ValueError("takeover 只能是 '' / 'password' / 'otp'")
    if takeover == "password" and not str(password or ""):
        raise ValueError("接管模式 password 必须提供密码")
    if not full_name:
        from core.name_samples import random_display_name

        full_name = random_display_name()
    if not age:
        age = random.randint(24, 51)

    with _ant_page(proxy, email=email) as (page, ctx, opened):
        deadline = time.time() + _SIGNUP_TIMEOUT
        # 入口必须与生产 Cloak 流一致：chatgpt.com/auth/login 带
        # login_or_signup 提示——直开 auth.openai.com/log-in（纯 login）
        # 会把新邮箱直接扔到 /log-in/password（实测三个新邮箱全中）。
        # 注册和接管都用它：2026-11-24 实测接管时直开 auth.openai.com/log-in 落到了
        # 「会话已失效（invalid_state）」页，而那页的「重试」绕回来的正是这个入口。
        page.goto("https://chatgpt.com/auth/login", timeout=60000, wait_until="domcontentloaded")
        time.sleep(4)
        _wait_out_challenge(page, timeout=75.0, step="reg_init")
        _heal_chrome_error(page)
        # chatgpt.com/auth/login 的内联邮箱表单会自动跳 auth.openai.com；
        # 两种形态都接受（选择器相同）。
        for _ in range(20):
            if _visible_css(page, _EMAIL_INPUT_SELECTORS):
                break
            if "auth.openai.com" in str(page.url or ""):
                break
            time.sleep(1.5)
        _wait_out_challenge(page, timeout=60.0, step="reg_entry")

        # ---- 1) 邮箱页 ----
        email_css = None
        session_retries = 0
        while time.time() < deadline:
            if _detect_challenge(page).get("active"):
                _wait_out_challenge(page, timeout=60.0, step="reg_email_wait")
                continue
            if "chrome-error" in str(page.url or ""):
                _heal_chrome_error(page)
                continue
            visible_css = _visible_css(page, _EMAIL_INPUT_SELECTORS)
            if visible_css:
                if _human_type(page, visible_css, email):
                    email_css = visible_css
                    break
                time.sleep(1.0)
                continue
            if _click_entry_button(page):
                time.sleep(5)
                continue
            if _session_expired_page(page):
                session_retries += 1
                if session_retries > 2:
                    raise RuntimeError("登录会话反复失效（invalid_state）：这个出口上登录入口不稳，换出口再试")
                if _force_click_by_text(page, _SESSION_RETRY_PATTERNS):
                    logger.info("[Ant注册] %s 登录会话已失效，已点「重试」(%s/2)", email, session_retries)
                    time.sleep(6)
                    continue
            time.sleep(1.5)
        if not email_css:
            raise RuntimeError(f"注册：邮箱输入未就绪 url={page.url[:80]}")
        time.sleep(0.6)
        email_submit_ts = time.time()
        _human_submit(page, _EMAIL_SUBMIT_SELECTORS)
        logger.info("[Ant注册] %s 邮箱已提交", email)

        # ---- 2) 分岔：email-verification（新邮箱）或直接 /log-in/password（邮箱已有账号） ----
        clicked_pw = False
        skip_password = False
        pw_link_wait_until = time.time() + 20    # 仅接管模式用：等「使用密码继续」的时限
        while time.time() < deadline:
            if _detect_challenge(page).get("active"):
                _wait_out_challenge(page, timeout=75.0, step="reg_otp_page")
                continue
            if "chrome-error" in str(page.url or ""):
                _heal_chrome_error(page)
                continue
            url = str(page.url or "")
            if "/log-in/password" in url and not _visible_css(page, _NAME_INPUT):
                if takeover:
                    logger.info("[Ant接管] %s 提交邮箱后直达登录密码页", email)
                    clicked_pw = True
                    break
                # 未经过验证码页直接进登录密码页 = 该邮箱已有 OpenAI 账号
                raise EmailAlreadyRegistered(
                    f"{email} 已有 OpenAI 账号（提交后直达 /log-in/password），换邮箱")
            # 有的会话提交邮箱后不经过验证码页，直接落到创建密码页（2026-11-23 实测，
            # 某条机房出口）。不认它就会干等到超时，把一步之遥的注册判成失败。
            if "/create-account/password" in url:
                logger.info("[Ant注册] %s 邮箱提交后直达创建密码页", email)
                clicked_pw = True
                break
            if takeover:
                if "chatgpt.com" in url and "/auth/" not in url:
                    logger.info("[Ant接管] %s 提交邮箱后已是登录态，不用再输密码", email)
                    clicked_pw = skip_password = True
                    break
                if ((takeover == "otp" or time.time() > pw_link_wait_until)
                        and _visible_css(page, _OTP_INPUT_SELECTORS)):
                    # 无密码接管：验证码页本身就是登录入口（登录验证码已自动发出）。
                    # 有密码但等不到「使用密码继续」的也走这里——验证码登录对有密码的账号同样可用。
                    logger.info("[Ant接管] %s 用一次性验证码登录", email)
                    clicked_pw = skip_password = True
                    break
            hit = _force_click_by_text(page, _PW_LINK_PATTERNS)
            if hit:
                logger.info("[Ant注册] %s 已点「使用密码继续」(%s)", email, hit)
                clicked_pw = True
                time.sleep(4)
                break
            time.sleep(1.5)
        if not clicked_pw:
            raise RuntimeError(f"注册：验证码页未出现密码入口 url={page.url[:80]}")

        # ---- 3) 密码页：/create-account/password=新号注册；/log-in/password=登录已有账号（仅接管） ----
        password_submit_ts = None
        if not skip_password:
            _submit_password(page, email, password, deadline, takeover=takeover)
            password_submit_ts = time.time()
            if progress is not None and not takeover:
                progress["password_submitted"] = True

        # ---- 3.5+4) 密码后统一等待循环（对齐登录尾段 6/6 验证的设计）：
        # 跳转链可能数分钟且中途撞 CF 质询；分四分支处理：
        #   email-verification+码框 → 邮箱验证码（取码真实键盘，可多轮）
        #   about-you → 姓名生日
        #   chatgpt.com 持续停留 → 已回站（老流程/免资料页）
        #   CF 质询 → 安静等待
        # 无密码接管时，验证码在提交邮箱那一刻就发出了，第 1 轮取码的时间下限要用邮箱提交时刻。
        otp_state = {"round": 0, "last_submit": 0.0,
                     "after_ts": email_submit_ts if skip_password else password_submit_ts}
        tail_deadline = time.time() + 480
        cg_steady_since = None
        name_filled = False
        while time.time() < tail_deadline:
            url = str(page.url or "")
            try:
                if _looks_like_mfa_page(page):
                    # 密码已过但撞上我们没绑过的 2FA 码页：账号带别人的
                    # authenticator，无秘密无法接管——按约定直接废弃邮箱。
                    raise EmailHasUnknown2FA(
                        f"{email} 出现未知 2FA 码页（我们未绑定过），账号无法接管 url={url[:80]}")
                if (takeover and password_submit_ts and "/log-in/password" in url
                        and time.time() - password_submit_ts > 12):
                    # 接管：密码提交 12s 后还停在密码页 = 密码没被接受，等下去没有意义
                    raise TakeoverLoginFailed("密码页停留超过 12s：密码未被接受")
                if _detect_challenge(page).get("active"):
                    cg_steady_since = None
                    _wait_out_challenge(page, timeout=75.0, step="reg_tail")
                    continue
                if "chrome-error" in url:
                    _heal_chrome_error(page)
                    continue
                if "email-verification" in url:
                    cg_steady_since = None
                    try:
                        fed = _feed_otp_round(page, email, otp_state)
                    except OtpRoundFailed:
                        raise
                    except Exception as exc:
                        # 第 1 轮没取到码：记一笔，留在循环里重试同一轮，直到尾段超时
                        logger.warning("[Ant注册] 创建 OTP 处理失败：%s", str(exc)[:110])
                        fed = False
                    if fed:
                        time.sleep(4)
                    time.sleep(2.0)
                    continue
                if "about-you" in url:
                    cg_steady_since = None
                    if _confirm_birthdate_dialog(page, email):
                        time.sleep(6)
                        continue
                    name_css = _visible_css(page, _NAME_INPUT)
                    if name_css:
                        if _human_type(page, name_css, full_name):
                            age_css = _visible_css(page, _AGE_INPUT)
                            if age_css:
                                _human_type(page, age_css, str(age))
                            time.sleep(0.6)
                            _human_submit(page, _EMAIL_SUBMIT_SELECTORS + ["button|||^continue$|^继续$|create account|创建"])
                            time.sleep(8)
                            if "about-you" not in str(page.url or ""):
                                name_filled = True
                                break
                            if _confirm_birthdate_dialog(page, email):
                                time.sleep(6)
                                continue
                            _human_submit(page, _EMAIL_SUBMIT_SELECTORS + ["button|||^continue$|^继续$|create account|创建"])
                            time.sleep(6)
                            if "about-you" not in str(page.url or ""):
                                name_filled = True
                                break
                    time.sleep(2.0)
                    continue
                if "chatgpt.com" in url and "/auth/" not in url:
                    if cg_steady_since is None:
                        cg_steady_since = time.time()
                    elif time.time() - cg_steady_since > 12:
                        name_filled = True  # 已回站（资料页被跳过）
                        break
                    time.sleep(2.0)
                    continue
                cg_steady_since = None
            except (EmailHasUnknown2FA, OtpRoundFailed, TakeoverLoginFailed):
                # 这些是明确的终局信号，必须冒泡。下面那条兜底只用来接页面抖动类异常——
                # 之前它们和普通异常一起被吞成一条 debug 日志，「撞上未知 2FA 就废弃邮箱」
                # 的约定因此从未真正生效（只会干等到超时）。
                raise
            except Exception as exc:
                logger.debug("[Ant注册] 尾段循环异常：%s", str(exc)[:100])
            time.sleep(1.5)

        if not name_filled:
            try:
                import json as _json

                state = page.evaluate("""() => {
                  const v = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                  const inputs = [...document.querySelectorAll('input')].filter(v)
                    .map(el => ({t: el.type, n: el.name, val: (el.value||'').length}));
                  return {url: location.href.slice(0, 90), inputs,
                          body: (document.body.innerText||'').slice(0, 250)};
                }""")
                logger.error("[Ant注册] 尾段失败现场: %s", _json.dumps(state, ensure_ascii=False)[:420])
                try:
                    page.screenshot(path="/app/cache/ant-reg-tail-fail.png")
                except Exception:
                    pass
            except Exception:
                pass
            if takeover:
                raise TakeoverLoginFailed(f"接管：登录后没有走完，停在 {page.url[:80]}")
            raise RegistrationAdvanced(
                f"注册已过密码页但未完成（半注册，OpenAI 侧已有账号+密码）url={page.url[:80]}",
                password=password)

        # ---- 5) 回站 → 取 token ----
        token = None
        user_id = user_name = expires = None
        user_obj = account_obj = None
        sess_deadline = time.time() + 90
        while time.time() < sess_deadline:
            url = str(page.url or "")
            if "chatgpt.com" in url and "/auth/" not in url and "chrome-error" not in url:
                try:
                    sess = page.evaluate("""async () => {
                        try {
                          const r = await fetch('/api/auth/session', {credentials: 'include'});
                          if (!r.ok) return null;
                          const j = await r.json();
                          if (!j.user) return null;
                          return {token: j.accessToken || null,
                                  uid: j.user.id || null,
                                  name: j.user.name || null,
                                  expires: j.expires || null,
                                  user: j.user || null,
                                  account: j.account || null};
                        } catch (e) { return null; }
                    }""")
                    if sess and (sess.get("token") or sess.get("uid")):
                        token = sess.get("token")
                        user_id = sess.get("uid")
                        user_name = sess.get("name")
                        expires = sess.get("expires")
                        user_obj = sess.get("user")
                        account_obj = sess.get("account")
                        break
                except Exception:
                    pass
            time.sleep(2.5)
        if not token and not user_id:
            raise RuntimeError(f"注册：未取到会话 token url={page.url[:80]}")
        logger.info("[Ant注册] %s 完成：user=%s token=%s", email, user_id, bool(token))
        return {
            "access_token": token or "",
            "user_id": user_id,
            "user_name": user_name,
            "expires": expires,
            # 完整 session 对象：入池时与其它驱动同构（save_account_data 从 account.planType 取套餐）
            "user": user_obj,
            "account": account_obj,
            "cookies": _export_cookies(ctx),
            "profile_id": opened.profile_id,
            "full_name": full_name,
            "age": age,
        }


# --------------------------------------------------------------------------
# run_registration 适配层：与 roxy/cloak 驱动同签名，供 main.py 分发调用。

def _driver_password() -> str:
    try:
        from config import register as _register_cfg
        configured = str(getattr(_register_cfg, "REGISTER_PASSWORD", "") or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    import string as _string
    chars = [random.choice(_string.ascii_uppercase), random.choice(_string.ascii_lowercase),
             random.choice(_string.digits), random.choice("!@#$%^&*")]
    pool = _string.ascii_letters + _string.digits + "!@#$%^&*"
    chars.extend(random.choice(pool) for _ in range(10))
    random.shuffle(chars)
    return "".join(chars)


def _age_from_birthday(birthday: str | None) -> int | None:
    import datetime as _dt
    try:
        bd = _dt.date.fromisoformat(str(birthday or "").strip()[:10])
        today = _dt.date.today()
        return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    except Exception:
        return None


def ant_proxy_candidates() -> list[str]:
    """代理池里 Ant 节点可达的出口（已按 ANT_PROXY_EXCLUDE 剔除）。"""
    import re as _re

    from config import antbrowser as _ant_cfg
    from config.proxy import PROXY_POOL

    markers = [m.strip().lower()
               for m in _re.split(r"[,\s]+", str(getattr(_ant_cfg, "ANT_PROXY_EXCLUDE", "") or ""))
               if m.strip()]
    return [p for p in PROXY_POOL if not any(m in str(p).lower() for m in markers)]


def pick_ant_proxy(explicit: str | None = None) -> str:
    """返回本轮要用的代理：显式传入的原样用，否则从节点可达的出口里随机抽一个。

    代理池是多流程共用的（本机协议流程 + HK 节点浏览器）：同一个出口可能
    从本机通、从节点 TCP 不通（实测确有这样的出口）。抽到这种
    出口只会在入口页干等到超时，所以只在 ant_proxy_candidates() 里抽。
    显式传入的不拦，由调用方负责。
    """
    proxy = str(explicit or "").strip()
    if proxy:
        return proxy
    pool = ant_proxy_candidates()
    if not pool:
        raise RuntimeError("Ant 驱动没有可用代理：代理池为空，或全部被 ANT_PROXY_EXCLUDE 排除")
    return random.choice(pool)


def run_ant_registration(
    email: str | None,
    name: str,
    birthday: str | None = None,
    proxy: str = None,
    otp_code: str = None,
    batch_dir=None,
    on_email_acquired=None,
) -> dict:
    """Ant Browser 驱动入口（对齐 run_roxy_registration / run_cloak_registration 的签名与返回结构）。

    邮箱按需领取（浏览器驱动惯例：找到输入框才领）；代理不传时从代理池抽一个
    节点可达的出口——节点裸 IP 被 OpenAI 边缘封禁，_ant_page 强制要求显式代理。
    注册后的 Codex / 2FA / 单席位探测与其它驱动走同一套开关和钩子。
    """
    from core.account_export import save_account_data
    from core.email_provider import (
        acquire_email_after_input, release_email, resolve_email_source,
    )

    # 提前初始化：异常分支要用它们决定邮箱去向，不能依赖 try 里的赋值顺序
    email = str(email or "").strip() or None
    progress: dict = {}
    password = ""

    try:
        email = acquire_email_after_input(email)
        if on_email_acquired:
            on_email_acquired(email)
        logger.info("[Ant注册] 驱动开始：%s proxy=%s", email, "explicit" if proxy else "pool")

        proxy = pick_ant_proxy(proxy)
        try:
            from urllib.parse import urlparse as _urlparse
            _pu = _urlparse(str(proxy))
            logger.info("[Ant注册] %s 本轮代理出口：%s", email, f"{_pu.hostname}:{_pu.port}")
        except Exception:
            pass

        password = _driver_password()
        # 密码先落日志：半注册失败时这是唯一可追溯处（copy_line 之外）
        logger.info("[Ant注册] %s 本轮 OpenAI 密码：%s", email, password)
        info = ant_register_flow(
            email,
            proxy=proxy,
            password=password,
            full_name=name or None,
            age=_age_from_birthday(birthday),
            progress=progress,
        )

        codex_result = _codex_step(email, proxy)

        row_id = save_account_data(
            email=email,
            access_token=info.get("access_token") or "",
            extra={
                "user": info.get("user") or {"id": info.get("user_id"), "name": info.get("user_name")},
                "account": info.get("account"),
                "expires": info.get("expires"),
                "ant_browser": {"profile_id": info.get("profile_id")},
                "registration_password": password,
                "codex": codex_result,
            },
            email_source=resolve_email_source(email),
            proxy_used=proxy,
            batch_dir=batch_dir,
        )
        logger.info("[Ant注册] %s 已入库：account_row=%s", email, row_id)
        # 2FA 不在这里排：save_account_data 在 ENABLE_2FA 开启时已按 registration_auto
        # 入队，与其它驱动一致、受同一个总开关控制。

        codex_ok = bool(codex_result.get("ok")) or codex_result.get("status") == "skipped"
        return {
            "success": codex_ok,
            "email": email,
            "account_id": row_id,
            "access_token": info.get("access_token") or "",
            "totp_secret": None,
            "codex": codex_result,
            "network_traffic": None,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except EmailAlreadyRegistered as exc:
        # 池邮箱在 OpenAI 侧已有账号：直接停用，防止反复领取反复失败
        logger.warning("[Ant注册] %s", exc)
        try:
            if email:
                release_email(email, status="disabled", note=str(exc)[:200])
        except Exception:
            pass
        return {"success": False, "email": email, "network_traffic": None,
                "error": f"EmailAlreadyRegistered: {exc}"}
    except EmailHasUnknown2FA as exc:
        # 已有我们不知道的 2FA：无秘密无法接管，邮箱按约定废弃
        logger.warning("[Ant注册] %s", exc)
        try:
            if email:
                release_email(email, status="disabled",
                              note=f"废弃：{str(exc)[:180]}")
        except Exception:
            pass
        return {"success": False, "email": email, "network_traffic": None,
                "error": f"EmailHasUnknown2FA: {exc}"}
    except RegistrationAdvanced as exc:
        # 半注册：OpenAI 侧已有账号+本轮密码，但邮箱验证/资料未完成。
        # 不回 available（会被再领去注册白撞），标 failed 留给补密码接管。
        pwd = getattr(exc, "password", "") or ""
        logger.error("[Ant注册] %s", exc)
        try:
            if email:
                release_email(
                    email, status="failed",
                    note=f"半注册待补密码接管 password={pwd}；{str(exc)[:120]}")
        except Exception:
            pass
        return {"success": False, "email": email, "network_traffic": None,
                "error": f"RegistrationAdvanced: {exc}"}
    except Exception as exc:
        advanced = bool(progress.get("password_submitted"))
        logger.error("[Ant注册] 失败：%s: %s", type(exc).__name__, str(exc)[:300])
        try:
            if email:
                if advanced:
                    # 密码已提交 = OpenAI 侧已用本轮密码建号（例如最后取会话令牌/入库
                    # 失败）。退回池子会被再领去撞「已有账号」然后停用，等于白丢一个号；
                    # 按半注册隔离，密码留在备注里供接管。
                    release_email(
                        email, status="failed",
                        note=f"已建号但流程未走完 password={password}；{str(exc)[:120]}")
                else:
                    release_email(email, status="available",
                                  note=f"Ant注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        # 已建号的失败沿用 RegistrationAdvanced 前缀，调用方（任务页/自动化脚本）
        # 据此就知道这个邮箱不能当「未消耗」处理
        prefix = "RegistrationAdvanced" if advanced else type(exc).__name__
        return {"success": False, "email": email, "network_traffic": None,
                "error": f"{prefix}: {str(exc)[:300]}"}
