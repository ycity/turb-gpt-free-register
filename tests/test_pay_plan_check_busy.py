# -*- coding: utf-8 -*-
"""支付后的延迟查套餐不能被重启静默打断。

问题（2026-11-18 线上，账号 #94）：
  支付成功后会起一个 daemon 线程，等 30 秒再查套餐——套餐信息和订阅编号
  都依赖这一步。但这个线程不在 /api/busy 的任何统计里：
    06:04:43  #94 付款成功，启动 30 秒延迟线程
    06:04:50  safe-restart.sh 报"当前空闲"，重启
              线程被杀，套餐查询从未执行，订阅编号一直空着

修法：等待期间登记进 _PENDING_PLAN_CHECKS，让 /api/busy 看得见。
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

from core import vcc_pay_session as ps  # noqa: E402


class PendingRegistryTest(unittest.TestCase):
    def setUp(self):
        with ps._plan_check_lock:
            ps._PENDING_PLAN_CHECKS.clear()

    tearDown = setUp

    def test_empty_by_default(self):
        self.assertEqual(ps.pending_plan_checks(), [])

    def test_registered_while_waiting(self):
        """等待期间要能被查到，否则重启看不见它。"""
        sess = ps.PaySession(sid="s1", account_id=94)
        cfg = MagicMock()
        cfg.VCC_AUTO_PLAN_CHECK = True
        cfg.VCC_AUTO_PLAN_CHECK_DELAY = 5.0
        with patch("threading.Thread"):      # 不真跑线程
            ps._schedule_plan_check(sess, cfg)
        pend = ps.pending_plan_checks()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["account_id"], 94)
        self.assertGreater(pend[0]["due_in"], 0)

    def test_cleared_after_run(self):
        """跑完要清掉，否则 /api/busy 会永远显示忙。"""
        sess = ps.PaySession(sid="s2", account_id=95)
        cfg = MagicMock()
        cfg.VCC_AUTO_PLAN_CHECK = True
        cfg.VCC_AUTO_PLAN_CHECK_DELAY = 0.0
        with patch("core.plan_check_service.enqueue_account_plan_check",
                   return_value={"accepted": True}), \
             patch("core.db.get_account",
                   return_value={"email": "a@e.test", "access_token": "t"}):
            ps._schedule_plan_check(sess, cfg)
            for _ in range(50):
                if not ps.pending_plan_checks():
                    break
                time.sleep(0.05)
        self.assertEqual(ps.pending_plan_checks(), [],
                         "跑完没清掉，busy 会一直为真")

    def test_cleared_even_on_error(self):
        """查套餐报错也要清掉登记。"""
        sess = ps.PaySession(sid="s3", account_id=96)
        cfg = MagicMock()
        cfg.VCC_AUTO_PLAN_CHECK = True
        cfg.VCC_AUTO_PLAN_CHECK_DELAY = 0.0
        with patch("core.db.get_account", side_effect=RuntimeError("boom")):
            ps._schedule_plan_check(sess, cfg)
            for _ in range(50):
                if not ps.pending_plan_checks():
                    break
                time.sleep(0.05)
        self.assertEqual(ps.pending_plan_checks(), [])

    def test_not_registered_when_disabled(self):
        sess = ps.PaySession(sid="s4", account_id=97)
        cfg = MagicMock()
        cfg.VCC_AUTO_PLAN_CHECK = False
        ps._schedule_plan_check(sess, cfg)
        self.assertEqual(ps.pending_plan_checks(), [])

    def test_uses_finally_for_cleanup(self):
        """清理必须在 finally 里，否则异常路径会漏。"""
        import inspect
        src = inspect.getsource(ps._schedule_plan_check)
        i = src.index("_PENDING_PLAN_CHECKS.pop")
        self.assertIn("finally:", src[:i])


class BusyEndpointTest(unittest.TestCase):
    def test_busy_counts_pending(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def api_busy")
        seg = src[i:i + 3000]
        self.assertIn("pending_plan_checks", seg)
        self.assertIn("pay_plan_checks", seg)

    def test_busy_flag_set(self):
        """光报数字不够，busy 必须变真，否则 safe-restart 还是会放行。"""
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("pending_plan_checks")
        seg = src[i:i + 500]
        self.assertIn("busy = busy or", seg)

    def test_reports_due_in(self):
        """报出还要等多久，人能判断值不值得等。"""
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        self.assertIn("pay_plan_checks_due_in", src)


if __name__ == "__main__":
    unittest.main()
