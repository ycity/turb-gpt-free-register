# -*- coding: utf-8 -*-
"""支付成功后自动查套餐。

支付提交成功不等于套餐已生效，OpenAI 侧写套餐有延迟，所以要等一会儿再查。
查套餐失败绝不能影响已经完成的支付。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vcc_pay_session as vps  # noqa: E402


def _cfg(**over):
    class Cfg:
        VCC_AUTO_PLAN_CHECK = True
        VCC_AUTO_PLAN_CHECK_DELAY = 0.0
    for k, v in over.items():
        setattr(Cfg, k, v)
    return Cfg()


def _sess(account_id=7, email="u@e.test"):
    return vps.PaySession(sid="s1", account_id=account_id, email=email)


_DEFAULT = object()


class SchedulePlanCheckTest(unittest.TestCase):
    def _run_and_wait(self, sess, cfg, *, account=_DEFAULT, enqueue=None):
        """跑调度并等后台线程结束，避免断言跑在它前面。"""
        done = threading.Event()
        calls = []

        def fake_enqueue(**kw):
            calls.append(kw)
            done.set()
            return (enqueue or {"accepted": True})

        acc = ({"id": sess.account_id, "email": sess.email, "access_token": "tok"}
               if account is _DEFAULT else account)

        with patch("core.db.get_account", return_value=acc), \
             patch("core.plan_check_service.enqueue_account_plan_check",
                   side_effect=fake_enqueue):
            vps._schedule_plan_check(sess, cfg)
            fired = done.wait(3.0)
        return fired, calls

    def test_enqueues_plan_check_after_payment(self):
        fired, calls = self._run_and_wait(_sess(), _cfg())
        self.assertTrue(fired, "没有入队套餐查询")
        self.assertEqual(calls[0]["account_id"], 7)
        self.assertEqual(calls[0]["access_token"], "tok")
        self.assertEqual(calls[0]["trigger"], "payment_auto",
                         "要能和手动查询区分开，便于排查")

    def test_disabled_does_nothing(self):
        fired, calls = self._run_and_wait(_sess(), _cfg(VCC_AUTO_PLAN_CHECK=False))
        self.assertFalse(fired)
        self.assertEqual(calls, [])

    def test_waits_configured_delay_before_querying(self):
        slept = []
        with patch.object(vps.time, "sleep", side_effect=slept.append), \
             patch("core.db.get_account",
                   return_value={"id": 7, "email": "u@e.test", "access_token": "t"}), \
             patch("core.plan_check_service.enqueue_account_plan_check",
                   return_value={"accepted": True}):
            vps._schedule_plan_check(_sess(), _cfg(VCC_AUTO_PLAN_CHECK_DELAY=30.0))
            for t in vps.threading.enumerate():
                if t.name.startswith("plan-after-pay-"):
                    t.join(3.0)
        self.assertIn(30.0, slept, "没有按配置延迟就去查了")

    def test_missing_token_is_skipped_quietly(self):
        fired, calls = self._run_and_wait(
            _sess(), _cfg(),
            account={"id": 7, "email": "u@e.test", "access_token": ""})
        self.assertFalse(fired)
        self.assertEqual(calls, [])

    def test_missing_account_is_skipped_quietly(self):
        fired, calls = self._run_and_wait(_sess(), _cfg(), account=None)
        self.assertFalse(fired)
        self.assertEqual(calls, [])

    def test_invalid_account_id_does_not_start_thread(self):
        before = threading.active_count()
        vps._schedule_plan_check(_sess(account_id=0), _cfg())
        self.assertEqual(threading.active_count(), before)

    def test_enqueue_failure_does_not_raise(self):
        """查套餐失败不能把已完成的支付搞崩。"""
        with patch("core.db.get_account",
                   return_value={"id": 7, "email": "u@e.test", "access_token": "t"}), \
             patch("core.plan_check_service.enqueue_account_plan_check",
                   side_effect=RuntimeError("队列炸了")):
            vps._schedule_plan_check(_sess(), _cfg())
            for t in threading.enumerate():
                if t.name.startswith("plan-after-pay-"):
                    t.join(3.0)
        # 没有异常冒出来即为通过

    def test_bad_delay_config_falls_back(self):
        slept = []
        with patch.object(vps.time, "sleep", side_effect=slept.append), \
             patch("core.db.get_account",
                   return_value={"id": 7, "email": "u@e.test", "access_token": "t"}), \
             patch("core.plan_check_service.enqueue_account_plan_check",
                   return_value={"accepted": True}):
            vps._schedule_plan_check(_sess(), _cfg(VCC_AUTO_PLAN_CHECK_DELAY="abc"))
            for t in threading.enumerate():
                if t.name.startswith("plan-after-pay-"):
                    t.join(3.0)
        self.assertIn(30.0, slept)


class TriggerPointTest(unittest.TestCase):
    def test_only_wired_to_the_success_path(self):
        """只在 ST_DONE 那一处触发，失败/取消不该查套餐。"""
        src = Path(vps.__file__).read_text(encoding="utf-8")
        body = src.split("def _schedule_plan_check", 1)[0] + \
            src.split("def _shoot", 1)[1]
        self.assertEqual(body.count("_schedule_plan_check(sess, cfg)"), 1)
        idx = src.index("_schedule_plan_check(sess, cfg)")
        window = src[max(0, idx - 300):idx]
        self.assertIn("ST_DONE", window, "触发点不在支付成功分支上")


if __name__ == "__main__":
    unittest.main()
