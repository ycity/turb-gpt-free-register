# -*- coding: utf-8 -*-
"""注册失败的邮箱回收：计数不能被 worker 抢跑绕过。

真实根因（KleinmannDriskoll26@outlook.com 连废 7 次）：
    - db 层本来就有 release_unconsumed_outlook：连续失败累加计数，
      到 _EMAIL_MAX_CONSECUTIVE_FAILURES(3) 就停用，不再放回池子。
    - 但注册 worker（cloak/roxy）在失败分支里自己先调了
      release_email(status="available")，把邮箱改回可用。
    - 于是 service 层再调 release_unconsumed_outlook 时，看到 status 已经
      不是 "used"，直接 return False，计数永远涨不上去——坏邮箱被反复领取。

修复：worker 失败时不自己回收，交给 registration_service 统一走计数路径。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class WorkerDoesNotSelfReleaseTest(unittest.TestCase):
    """worker 失败分支不能再自己 release_email，否则绕过计数。"""

    def test_cloak_no_self_release(self):
        src = _src("core/cloakbrowser_registration.py")
        # 失败分支里不该出现把邮箱改回 available 的调用
        self.assertNotIn('release_email(email, status="failed" if create_acknowledged', src)

    def test_roxy_no_self_release(self):
        src = _src("core/roxy_registration.py")
        self.assertNotIn('release_email(email, status="failed" if create_acknowledged', src)

    def test_cloak_explains_why(self):
        """得留注释说明为什么不自己回收，否则有人会"顺手修回来"。"""
        src = _src("core/cloakbrowser_registration.py")
        i = src.index("[Cloak注册] 失败：%s")
        self.assertIn("registration_service", src[i:i + 500])
        self.assertIn("计数", src[i:i + 500])


class ServiceCountsFailuresTest(unittest.TestCase):
    """service 层仍然对每个未消耗的失败走计数回收。"""

    def test_release_path_present(self):
        src = _src("core/registration_service.py")
        self.assertIn("_release_unconsumed_job_email", src)

    def test_db_counter_has_threshold(self):
        src = _src("core/db.py")
        m = re.search(r"_EMAIL_MAX_CONSECUTIVE_FAILURES\s*=\s*(\d+)", src)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 2)

    def test_unconsumed_disables_at_threshold(self):
        """到阈值要 disabled，而不是继续放回 available。"""
        src = _src("core/db.py")
        i = src.index("def release_unconsumed_outlook")
        body = src[i:i + 1200]
        self.assertIn("_EMAIL_MAX_CONSECUTIVE_FAILURES", body)
        self.assertIn('"disabled"', body)

    def test_success_clears_counter(self):
        """注册成功要清零计数，别让偶发失败累积成停用。"""
        src = _src("core/registration_service.py")
        self.assertIn("clear_outlook_failures", src)


class DeadEmailSignatureTest(unittest.TestCase):
    """OTP 输入框那类错误交给计数，不一次就停用。"""

    def test_otp_missing_not_instant_disable(self):
        src = _src("core/registration_service.py")
        i = src.index("def _should_disable_failed_registration_email")
        body = src[i:i + 900]
        # 明确不把"找不到 OTP 输入框"列入即时停用
        self.assertNotIn("OTP 输入框", body.split("return")[1]
                         if "return" in body else body)

    def test_login_password_still_instant_disable(self):
        """已注册过（进 /log-in/password）仍要即时停用。"""
        src = _src("core/registration_service.py")
        i = src.index("def _should_disable_failed_registration_email")
        body = src[i:i + 900]
        self.assertIn("/log-in/password", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
