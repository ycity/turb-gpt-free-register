# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data, post_register_dwell
from core.browser_data_saver import BrowserDataSaver
from core.browser_traffic import PlaywrightTrafficTracker, merge_proxy_transport_traffic
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import acquire_email_after_input, wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
    _email_otp_page_state,
    recover_session_after_token_failure,
)

logger = logging.getLogger(__name__)

# 点过"重新发送验证码"后，取码的基准时间往回放多少秒。
#
# OpenAI 对同一地址短时间内不会真的再发一封邮件，点了重发往往什么都没发生。
# 如果基准时间卡在"点击重发的那一刻"，刚收到的那封就变成"旧邮件"被跳过，
# 于是一直等不到码、整单失败——#172/#173/#174 连续三次都是这么挂的
# （日志里同一封 08:28:18 的邮件被反复判成 after=False）。
# 180s 足够覆盖"上一封还能用"的窗口，又不会吃到更早的历史验证码。
_OTP_RESEND_LOOKBACK = 180.0


def _try_resend_email_otp(driver) -> bool:
    """点"重新发送验证码"，点不到也不要让整单失败。

    原来直接调 _click_resend_email_otp，找不到按钮就抛 RuntimeError，
    外层没接住，整单就挂了（#174 最后那条
    "找不到可点击的重新发送验证码按钮" 就是这么来的）。

    但点不到按钮不等于没救：邮箱里往往已经有一封能用的验证码，
    下一轮按放宽后的 after_ts 就能取到。所以这里只记日志、不抛。
    """
    try:
        _click_resend_email_otp(driver, timeout=25)
        return True
    except Exception as exc:  # noqa: BLE001 - 点不到就靠下一轮取旧码
        logger.warning(
            "[Cloak注册][OTP] 没点到重新发送按钮（%s），"
            "继续用已放宽的时间窗等码：%s",
            type(exc).__name__, str(exc)[:160])
        return False


def run_cloak_registration(
    email: str | None,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    traffic_tracker: PlaywrightTrafficTracker | None = None
    data_saver: BrowserDataSaver | None = None
    network_traffic: dict | None = None
    try:
        # 按账号隔离指纹时需要账号标识。注意邮箱可能要等页面出现输入框后才领取
        # （_email_supplier_after_input），那种情况下这里拿到的是空串，会退回
        # 临时资料目录——仍然不会和别的账号共用设备身份。
        driver, opened = build_cloak_driver(proxy=proxy, identity=str(email or ""))
        try:
            traffic_tracker = PlaywrightTrafficTracker(driver.context, label="Cloak")
        except Exception as exc:
            # 统计失败不应影响注册主流程。
            logger.warning("[Cloak注册] 初始化浏览器流量统计失败，继续注册：%s: %s", type(exc).__name__, str(exc)[:180])
        data_saver = BrowserDataSaver(label="Cloak")
        if traffic_tracker is not None:
            traffic_tracker.attach_data_saver(data_saver)
        data_saver.install_playwright(driver.context)
        logger.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        driver.get("https://chatgpt.com/auth/login")
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()

        def _email_supplier_after_input() -> str:
            nonlocal email
            _check_manual_stop()
            email = acquire_email_after_input(email)
            if on_email_acquired:
                on_email_acquired(email)
            return email

        next_state = _submit_email_and_wait_next(
            driver,
            email,
            attempts=3,
            email_supplier=_email_supplier_after_input,
        )
        _check_manual_stop()

        # 如果邮箱提交后直接进入验证码页，也尝试点击“使用密码继续”进入密码创建页；
        # _fill_password_page_if_present 会在设置成功后返回本次 OpenAI 注册密码。
        openai_password = _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    # 同上：重发未必真的产生新邮件，基准时间要留回看窗口，
                    # 否则下一轮还是只能看到同一封"旧"邮件然后再次超时。
                    otp_after_ts = time.time() - _OTP_RESEND_LOOKBACK
                    _try_resend_email_otp(driver)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
                logger.info("[Cloak注册][OTP] 已提交验证码 %s，等待页面跳转",
                            current_otp)
            except Exception as exc:
                logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            # 10s 太短：实测这一步要过 Cloudflare 再跳转，慢的时候 20s 开外。
            # 误判成 invalid 的代价很大——会去点"重新发送"，而 OpenAI 对同一
            # 地址短时间内不会真的再发一封，于是旧邮件被 after_ts 挡住、
            # 陷入死等，最后整单失败（#172/#173/#174 都是这么挂的）。
            outcome = _wait_after_email_otp_submit(driver, timeout=30)
            if outcome == "accepted":
                break
            # 判成 invalid 时必须留下判据：这一步原来完全静默，
            # 日志里只能看到"收到验证码"后隔十几秒突然去点重发，
            # 根本分不清是验证码真错、页面被打回，还是误判。
            try:
                snap = _email_otp_page_state(driver)
            except Exception:  # noqa: BLE001 - 取快照失败不影响主流程
                snap = {}
            logger.warning(
                "[Cloak注册][OTP] 验证码 %s 被判定无效（第 %s/%s 次）"
                "url=%s errors=%s inputs=%s",
                current_otp, otp_attempt, max_otp_attempts,
                str(snap.get("url") or "?")[:90],
                str(snap.get("errors") or [])[:200],
                [{"ariaInvalid": i.get("ariaInvalid"), "value": i.get("value")}
                 for i in (snap.get("inputs") or [])][:4])
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            # 重发前把基准时间往回放一点：OpenAI 常常不会真发新邮件，
            # 此时只能复用刚才那封。往回 180s 足够覆盖"上一封还能用"的窗口，
            # 又不会吃到更早的历史验证码。
            otp_after_ts = time.time() - _OTP_RESEND_LOOKBACK
            _try_resend_email_otp(driver)
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        try:
            session_info = _fetch_chatgpt_session(driver, timeout=120)
        except Exception as exc:
            # 资料页已提交说明账号在 OpenAI 侧建好了，只是浏览器没接到 token；
            # 直接判失败会白白废掉一个可用账号，这里改用重新登录补取。
            if not create_acknowledged:
                raise
            session_info = recover_session_after_token_failure(
                email, (opened.raw or {}).get("proxy_pool_target") if opened else proxy, exc)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)

        if _twofa_cfg.ENABLE_2FA:
            logger.warning("[Cloak注册] 当前 CloakBrowser 自动化路径暂不执行 2FA 设置，已跳过")
        totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        # 统计注册浏览器关闭前的完整会话；注册后停留期间的网络请求也计入。
        post_register_dwell(email, label="Cloak注册")
        if traffic_tracker is not None:
            network_traffic = traffic_tracker.stop()
        network_traffic = merge_proxy_transport_traffic(network_traffic, driver.proxy_transport_snapshot(), label="Cloak")
        if data_saver is not None:
            data_saver.stop()
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy_pool_target") if opened else None) or proxy or None,
            proxy_region=((opened.raw or {}).get("proxy_region") if opened else None) or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
                "network_traffic": network_traffic,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "network_traffic": network_traffic,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        if traffic_tracker is not None:
            try:
                network_traffic = traffic_tracker.stop()
            except Exception:
                pass
        if driver is not None:
            network_traffic = merge_proxy_transport_traffic(network_traffic, driver.proxy_transport_snapshot(), label="Cloak")
        if data_saver is not None:
            data_saver.stop()
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        # 这里不要自己回收邮箱。registration_service 收到 success=False 后会
        # 统一处理：按错误类型决定停用还是放回，放回时走 release_unconsumed_*
        # 累加连续失败计数、到阈值自动停用。
        # worker 抢先把邮箱改回 available 会让那套计数失效——
        # release_unconsumed_outlook 看到 status 已不是 used 就直接返回，
        # 计数永远涨不上去，坏邮箱于是被反复领取（KleinmannDriskoll26 连废 7 次）。
        return {
            "success": False,
            "email": email,
            "network_traffic": network_traffic,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        if traffic_tracker is not None:
            try:
                traffic_tracker.stop()
            except Exception:
                pass
        if data_saver is not None:
            data_saver.stop()
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
