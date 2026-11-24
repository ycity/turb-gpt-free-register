# -*- coding: utf-8 -*-
"""注册时 OTP 重发不能把自己卡死。

实据（#172/#173/#174 连续三次失败，同一个邮箱）：
    08:27:31  打开登录页，otp_after_ts = 此刻
    08:28:18  OTP 邮件到达
    08:29:04  after=True，取到验证码 809092
    08:29:12  输入验证码
    08:29:24  判定 invalid，点"重新发送"，otp_after_ts 更新到此刻
    08:29:31+ 同一封 08:28:18 的邮件被反复判成 after=False
    08:31:24  找不到重发按钮 -> 整单失败

三个缺陷叠加：
    1. 提交后只等 10s，过 Cloudflare 慢一点就被误判成验证码错误
    2. 重发时把 otp_after_ts 设成"当下"，但 OpenAI 常常不会真发新邮件，
       刚收到那封立刻变成"旧邮件"，于是死等
    3. 重发按钮点不到直接抛错，整单失败——其实邮箱里有码可用

日志统计佐证：收码1+重发1 -> 失败，收码1+重发0 -> 成功。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 只读源码不导入模块：cloakbrowser_registration 会拉起 pyotp 等依赖，
# 本地开发机不一定装齐，没必要为了几条静态断言要求整套环境。
SRC = (Path(__file__).resolve().parents[1] / "core"
       / "cloakbrowser_registration.py").read_text(encoding="utf-8")


def _const(name: str) -> float:
    m = re.search(rf"^{name}\s*=\s*([\d.]+)", SRC, re.M)
    if not m:
        raise AssertionError(f"找不到常量 {name}")
    return float(m.group(1))


def _fn(name: str) -> str:
    i = SRC.index(f"def {name}(")
    nxt = SRC.find("\ndef ", i + 1)
    return SRC[i:nxt if nxt > 0 else len(SRC)]


class ResendLookbackTest(unittest.TestCase):
    def test_constant_exists(self):
        self.assertGreater(_const("_OTP_RESEND_LOOKBACK"), 0)

    def test_lookback_long_enough(self):
        """要覆盖"上一封还能用"的窗口。实测邮件到达与重发相隔约 60s，
        太短又会死等。"""
        self.assertGreaterEqual(_const("_OTP_RESEND_LOOKBACK"), 120)

    def test_lookback_not_unbounded(self):
        """放太宽会吃到更早的历史验证码，输进去必然失败。"""
        self.assertLessEqual(_const("_OTP_RESEND_LOOKBACK"), 600)

    def test_no_bare_now_before_resend(self):
        """重发前不能把基准时间设成"当下"——这正是死等的根因。"""
        body = _fn("run_cloak_registration")
        # 首次那条后面没有紧跟重发，是合法的；这里只看重发路径
        for m in re.finditer(r"otp_after_ts = time\.time\(\)\n", body):
            after = body[m.end():m.end() + 300]
            self.assertNotIn("_try_resend_email_otp", after,
                             "重发前把基准时间设成了当下，会死等")

    def test_both_resend_paths_use_lookback(self):
        """两条重发路径都要放宽：等码超时的那条，和验证码被拒的那条。"""
        body = _fn("run_cloak_registration")
        self.assertEqual(
            body.count("otp_after_ts = time.time() - _OTP_RESEND_LOOKBACK"), 2)

    def test_initial_ts_is_plain_now(self):
        """首次取码仍要用当下时间，不能也放宽——否则会吃到上一单的码。"""
        body = _fn("run_cloak_registration")
        first = body.index("otp_after_ts = time.time()")
        # 首次赋值之前不该出现放宽常量
        self.assertNotIn("_OTP_RESEND_LOOKBACK", body[:first])
        # 且它本身是裸的 time.time()，不带减法
        self.assertNotIn("-", body[first:first + 40].split("\n")[0])


class SubmitTimeoutTest(unittest.TestCase):
    def test_wait_longer_after_submit(self):
        """10s 太短：这一步要过 Cloudflare 再跳转，慢时 20s 开外。
        误判的代价是触发重发，进而死等。"""
        i = SRC.index("_wait_after_email_otp_submit(driver")
        self.assertIn("timeout=30", SRC[i:i + 80])

    def test_reason_documented(self):
        """为什么从 10 改到 30 要写清楚，否则会被人改回去。"""
        i = SRC.index("_wait_after_email_otp_submit(driver")
        self.assertIn("Cloudflare", SRC[max(0, i - 500):i])


class ResendFailureTolerantTest(unittest.TestCase):
    def test_helper_exists(self):
        self.assertIn("def _try_resend_email_otp", SRC)

    def test_helper_swallows_errors(self):
        """点不到按钮不等于没救：邮箱里往往已有可用的码。"""
        src = _fn("_try_resend_email_otp")
        self.assertIn("except Exception", src)
        self.assertIn("return False", src)
        self.assertNotIn("raise", src)

    def test_registration_uses_helper(self):
        """主流程不能再直接调会抛错的那个，否则整单还是会挂。"""
        body = _fn("run_cloak_registration")
        self.assertNotIn("_click_resend_email_otp(driver", body)
        self.assertIn("_try_resend_email_otp(driver)", body)

    def test_helper_logs_warning(self):
        """吞掉异常必须留日志，否则排查时完全看不见。"""
        self.assertIn("logger.warning", _fn("_try_resend_email_otp"))


class ResendSelectorTest(unittest.TestCase):
    """重发按钮认错比认不出更糟。

    #175 实据：日志写"已点击重新发送验证码按钮：再試一次"，
    18 秒后 url 变成 auth.openai.com/log-in，OTP 输入框消失，整单失败。
    "再試一次 / Try again" 是错误页的重试，点了会丢掉整个注册会话。
    正常重发按钮是 Resend email / 重新傳送電子郵件 / メールを再送信。
    """

    @staticmethod
    def _src() -> str:
        p = (Path(__file__).resolve().parents[1] / "core"
             / "roxy_registration.py").read_text(encoding="utf-8")
        i = p.index("def _click_resend_email_otp")
        return p[i:p.index("\ndef ", i + 1)]

    def test_no_bare_again_in_attrs(self):
        """裸的 again 会命中 try-again 按钮的属性。"""
        src = self._src()
        self.assertNotIn("new.*code|again", src)

    def test_excludes_try_again(self):
        src = self._src()
        self.assertIn("isRetry", src)
        self.assertIn("再試一次", src)

    def test_retry_filter_applied_to_both_paths(self):
        """属性匹配和文本兜底两条路都要排除，漏一条就还会误点。"""
        src = self._src()
        self.assertEqual(src.count("isRetry(el)"), 2)

    def test_keeps_real_resend_wordings(self):
        """实际见过的重发文案不能被误伤。"""
        src = self._src()
        for word in ("resend", "重新傳送", "再送信"):
            self.assertIn(word, src)

    def test_reason_documented(self):
        self.assertIn("log-in", self._src())


class OtpSubmitLoggingTest(unittest.TestCase):
    """判成 invalid 必须留判据。

    原来这一步完全静默：日志里只看到"收到验证码"后隔十几秒突然去点重发，
    分不清是验证码真错、页面被打回，还是误判——排查全靠猜。
    """

    def test_logs_submit(self):
        self.assertIn("已提交验证码", SRC)

    def test_logs_invalid_evidence(self):
        self.assertIn("被判定无效", SRC)

    def test_evidence_includes_url_and_errors(self):
        i = SRC.index("被判定无效")
        blk = SRC[max(0, i - 400):i + 500]
        self.assertIn("url=", blk)
        self.assertIn("errors=", blk)

    def test_snapshot_failure_tolerated(self):
        """取快照失败不能把主流程带崩。"""
        i = SRC.index("_email_otp_page_state(driver)")
        self.assertIn("except Exception", SRC[i:i + 200])


if __name__ == "__main__":
    unittest.main(verbosity=2)
