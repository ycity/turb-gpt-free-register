# -*- coding: utf-8 -*-
"""Codex 授权重试时已用密码+MFA 登录成功，就不该再等邮箱验证码。

事故（2026-11-06）：#62 / #64 / #65 跑 Codex 全部失败，报错是
「等待 Codex callback 超时，最后 URL=.../mfa-challenge/...」。

真实原因：_restart_email_otp_flow 是嵌套函数，它内部检测到密码 + MFA
已登录成功后执行 return，但那只退出嵌套函数本身；外层 for 循环拿不到
这个信息，continue 之后继续等邮箱验证码。而账号此时已经登录完成，
OpenAI 不会再发验证码邮件，于是必然等到超时。

日志里的矛盾序列：
    已填写并提交 MFA 验证码
    重新提交邮箱后已用密码完成登录，进入后续步骤   <- 登录成功
    等待邮箱 OTP（第 3/3 次）                      <- 却还在等邮件
    OutlookClientError: 等待 OTP 超时（>90s）

#60 没踩到是因为它第一次就走密码分支，直接 return 退出整个函数，
根本没进 for 循环。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
ROXY = (ROOT / "core" / "roxy_codex_oauth.py").read_text(encoding="utf-8")
BU = (ROOT / "core" / "browser_use_codex_oauth.py").read_text(encoding="utf-8")

DRIVERS = (("roxy", ROXY), ("browser_use", BU))


class FlagDeclaredTest(unittest.TestCase):
    """嵌套函数要把「已登录」这个状态传出来。"""

    def test_flag_initialised(self):
        for name, src in DRIVERS:
            self.assertIn("logged_in_via_password = False", src,
                          f"{name} 没有初始化标记")

    def test_flag_is_nonlocal(self):
        """不声明 nonlocal 的话赋值只会创建局部变量，外层永远读不到。"""
        for name, src in DRIVERS:
            self.assertIn("nonlocal otp_after_ts, logged_in_via_password", src,
                          f"{name} 没把标记声明成 nonlocal")

    def test_flag_set_on_password_login(self):
        for name, src in DRIVERS:
            self.assertIn("logged_in_via_password = True", src,
                          f"{name} 登录成功时没有置位")

    def test_flag_set_before_return(self):
        """置位必须在 return 之前，否则这行代码根本不会执行。"""
        for name, src in DRIVERS:
            idx = src.index("logged_in_via_password = True")
            tail = src[idx:idx + 200]
            self.assertIn("return", tail, f"{name} 置位后没有 return")
            # 置位语句本身不能写在 return 后面
            before_return = tail[:tail.index("return")]
            self.assertIn("logged_in_via_password = True", before_return)


class AllCallSitesCheckedTest(unittest.TestCase):
    """每个调用 _restart_email_otp_flow 的地方都要检查标记，漏一处就还会卡。"""

    def test_every_call_site_checks_flag(self):
        for name, src in DRIVERS:
            calls = [m.start() for m in
                     re.finditer(r"_restart_email_otp_flow\(\"", src)]
            self.assertGreaterEqual(len(calls), 2,
                                    f"{name} 的调用点少于预期")
            for pos in calls:
                window = src[pos:pos + 320]
                self.assertIn("logged_in_via_password", window,
                              f"{name} 在 offset {pos} 的调用点没检查标记")

    def test_returns_instead_of_continue(self):
        """检查到已登录要 return 退出整个流程，不能只是 continue。"""
        for name, src in DRIVERS:
            for m in re.finditer(r"if logged_in_via_password:", src):
                window = src[m.start():m.start() + 260]
                self.assertIn("return", window,
                              f"{name} 检查到已登录却没有 return")


class IncidentDocumentedTest(unittest.TestCase):
    def test_reason_in_comments(self):
        """写清楚为什么要这个标记，不然下次会被当成冗余代码删掉。"""
        for name, src in DRIVERS:
            idx = src.index("logged_in_via_password = False")
            comment = src[max(0, idx - 300):idx]
            self.assertIn("邮箱", comment, f"{name} 没说明原因")

    def test_nested_return_pitfall_noted(self):
        for name, src in DRIVERS:
            idx = src.index("logged_in_via_password = True")
            comment = src[max(0, idx - 300):idx]
            self.assertIn("嵌套函数", comment,
                          f"{name} 没点明嵌套函数 return 的陷阱")


class MfaFailureNotTreatedAsSuccessTest(unittest.TestCase):
    """MFA 验证码没通过时，不能当成登录成功继续往下走。

    事故（2026-11-06）：#62 修掉邮箱 OTP 的问题后仍然失败，日志是
        已填写并提交 MFA 验证码
        已填写并提交 MFA 验证码        <- 提交了两次
        账号已用密码完成登录，直接进入后续步骤  <- 却报成功
        未检测到手机号验证页，跳过手机步骤
        等待 Codex callback 超时，最后 URL=.../mfa-challenge/...

    _fill_mfa_challenge_if_present 等 12 秒后页面还在 MFA 页，却 return
    True。调用方据此认为登录完成，跳过后续步骤去等一个永远不会来的
    callback，最后 3 分钟超时。
    """

    def test_timeout_does_not_return_true(self):
        idx = ROXY.index("def _fill_mfa_challenge_if_present")
        body = ROXY[idx:idx + 3000]
        tail = body[body.index("已填写并提交 MFA 验证码"):]
        # 等待窗口结束后不能直接 return True
        self.assertNotIn("time.sleep(0.4)\n            return True", tail,
                         "超时仍返回 True，调用方会误判登录成功")

    def test_retries_with_fresh_code(self):
        """TOTP 每 30 秒一换，重试必须取新码，重复提交同一个没有意义。"""
        idx = ROXY.index("def _fill_mfa_challenge_if_present")
        body = ROXY[idx:idx + 3000]
        self.assertIn("tried", body, "没有记录已试过的验证码")
        self.assertIn("if code in tried:", body, "会重复提交同一个验证码")

    def test_code_fetched_inside_loop(self):
        """进函数时取一次的话，页面加载慢就会提交一个已过期的码。"""
        idx = ROXY.index("def _fill_mfa_challenge_if_present")
        body = ROXY[idx:idx + 3000]
        loop_at = body.index("while time.time() < end:")
        after_loop = body[loop_at:]
        self.assertIn("code = _account_totp_code_for_email(email)", after_loop,
                      "验证码不是在循环里现取的")

    def test_timeout_spans_totp_window(self):
        """TOTP 窗口 30 秒，超时必须够长才能等到下一个码。"""
        m = re.search(r"def _fill_mfa_challenge_if_present\([^)]*timeout: int = (\d+)",
                      ROXY)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 60,
                                "超时太短，跨不过一个 TOTP 窗口")

    def test_password_step_reports_mfa_failure(self):
        self.assertIn('return "mfa_failed"', ROXY,
                      "密码步骤没有把 MFA 失败这个状态传出来")

    def test_mfa_failure_raises(self):
        """必须抛错，不能静默 return——静默会让上层以为流程正常走完。"""
        self.assertIn("MFA 验证码连续未通过", ROXY)
        idx = ROXY.index("if mfa_failed:")
        self.assertIn("raise RuntimeError", ROXY[idx:idx + 220])

    def test_raise_is_outside_try(self):
        """try 里面抛会被 except Exception 吞掉变成静默 return。"""
        raise_at = ROXY.index("MFA 验证码连续未通过")
        except_at = ROXY.index(
            "logger.info(\"[Codex][Browser] 未检测到邮箱输入框")
        self.assertGreater(raise_at, except_at,
                           "raise 写在了 try 内部，会被 except 吞掉")

    def test_failure_skips_passwordless_fallback(self):
        """MFA 失败后不该再去点 passwordless，页面还停在 challenge 页。"""
        self.assertIn("elif not mfa_failed and pw_result != \"next_step\":", ROXY)


class MfaPageDetectionTest(unittest.TestCase):
    """SPA 验证通过后 URL 不变，不能只看 URL 判断还在不在 MFA 页。

    事故（2026-11-06）：#62 的 MFA 验证码其实通过了 ——
        POST /api/accounts/mfa/verify -> 200
        continue_url: https://auth.openai.com/add-phone
        page.type: "add_phone"
    页面已经是「需要电话号码」，但 URL 仍是 /mfa-challenge/6aebd2b5...，
    而检测函数第一句就是 `if "/mfa-challenge/" in url: return True`，
    于是判定仍在 MFA 页，反复重填验证码直到超时。
    """

    def test_roxy_does_not_shortcut_on_url(self):
        idx = ROXY.index("def _is_mfa_challenge_page")
        body = ROXY[idx:idx + 1600]
        head = body[:body.index("driver.execute_script")]
        self.assertNotIn("return True", head,
                         "仍然只看 URL 就返回 True")

    def test_browser_use_does_not_shortcut_on_url(self):
        idx = BU.index("def _looks_mfa_challenge_page")
        body = BU[idx:idx + 1200]
        head = body[:body.index("page.locator")]
        self.assertNotIn("return True", head,
                         "仍然只看 URL 就返回 True")

    def test_both_check_dom_first(self):
        """判断依据必须是验证码输入框还在不在，而不是 URL。"""
        idx = ROXY.index("def _is_mfa_challenge_page")
        self.assertIn('input[name="code"]', ROXY[idx:idx + 1600])
        idx = BU.index("def _looks_mfa_challenge_page")
        self.assertIn('input[name="code"]', BU[idx:idx + 1200])

    def test_url_kept_as_fallback(self):
        """DOM 取不到时还是要有兜底，但只能当兜底。"""
        for name, src, fn in (("roxy", ROXY, "_is_mfa_challenge_page"),
                              ("browser_use", BU, "_looks_mfa_challenge_page")):
            idx = src.index(f"def {fn}")
            body = src[idx:idx + 1600]
            tail = body[body.index("except Exception"):]
            self.assertIn("/mfa-challenge", tail,
                          f"{name} 没有 URL 兜底")

    def test_spa_pitfall_documented(self):
        for name, src, fn in (("roxy", ROXY, "_is_mfa_challenge_page"),
                              ("browser_use", BU, "_looks_mfa_challenge_page")):
            idx = src.index(f"def {fn}")
            doc = src[idx:idx + 700]
            self.assertIn("SPA", doc, f"{name} 没写明 SPA 这个坑")


if __name__ == "__main__":
    unittest.main(verbosity=2)
