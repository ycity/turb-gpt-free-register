# -*- coding: utf-8 -*-
"""安全重启脚本：有后台任务在跑时必须拒绝。

两次真实事故：
  - 账号 #42 的 2FA 被 docker restart 打断，因为当时的检查只看注册任务；
  - 注册任务 #113 被打断，因为临时脚本只打印了 /api/busy 的结果就继续执行。
所以把检查固化成仓库里的脚本，并且必须是"拦截"而不是"提示"。
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "safe-restart.sh"


class ScriptPresenceTest(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue(SCRIPT.is_file(), "部署检查必须固化成脚本，不能每次临时拼")

    def test_uses_lf_endings(self):
        raw = SCRIPT.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 0, "CRLF 会让 bash 报 syntax error")

    def test_bash_syntax_ok(self):
        import os
        import shutil
        if os.name == "nt":
            # Windows 下 Git Bash 收不到原生路径（E:\a\b 会被吃掉反斜杠），
            # 语法检查在部署时于服务器上做。
            raise unittest.SkipTest("Windows 下跳过，部署时在服务器校验")
        bash = shutil.which("bash")
        if not bash:
            raise unittest.SkipTest("本机没有 bash")
        r = subprocess.run([bash, "-n", str(SCRIPT)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class GuardLogicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SCRIPT.read_text(encoding="utf-8")

    def test_busy_exits_without_restarting(self):
        """关键：busy 分支必须在真正执行重启之前退出。

        注意不能用 "docker restart" 定位——文件开头的注释里就出现过这个词。
        这里找真正的执行行。
        """
        busy_exit = self.src.index("exit 10")
        restart = self.src.index('docker restart "$CONTAINER"')
        self.assertLess(busy_exit, restart,
                        "拒绝逻辑必须在重启之前，否则等于只是打印警告")

    def test_queries_busy_endpoint(self):
        self.assertIn("/api/busy", self.src)

    def test_force_flag_supported(self):
        self.assertIn("--force", self.src)
        self.assertIn("FORCE=1", self.src)

    def test_wait_flag_supported(self):
        self.assertIn("--wait", self.src)

    def test_unknown_state_is_not_treated_as_idle(self):
        """查不到状态时不能假定空闲——容器可能已挂，也可能只是接口超时。"""
        self.assertIn("unknown", self.src)
        self.assertIn("exit 11", self.src)

    def test_shows_what_is_running(self):
        """拒绝时要说明是什么在跑，否则用户不知道该等还是该 force。"""
        self.assertIn("show_detail", self.src)

    def test_waits_for_readiness_after_restart(self):
        self.assertIn("/api/summary", self.src)
        self.assertIn("ready after", self.src)

    def test_restart_failure_is_reported(self):
        seg = self.src[self.src.index('docker restart "$CONTAINER"'):]
        self.assertIn("exit 1", seg)


if __name__ == "__main__":
    unittest.main()
