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


if __name__ == "__main__":
    unittest.main(verbosity=2)
