# -*- coding: utf-8 -*-
"""自动注册调度的判定逻辑。

全部用 patch 隔离 DB 和 submit_registration，不触碰真实数据库和注册流程。
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from core import auto_register_scheduler as sched


def _ts(offset_seconds: float) -> str:
    return (datetime.now() - timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


class AutoRegisterEvaluateTests(unittest.TestCase):
    def _patch(self, *, enabled=True, available=5, job_counts=None, twofa=None, anchor=None):
        """装上判定所需的全部外部依赖，返回 ExitStack 以便在 with 中使用。"""
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch.object(sched, "is_enabled", return_value=enabled))
        stack.enter_context(patch.object(sched, "available_emails", return_value=available))
        stack.enter_context(patch.object(
            sched.db, "job_status_counts", return_value=job_counts or {"active": 0}))
        stack.enter_context(patch.object(
            sched.db, "totp_setup_activity",
            return_value=twofa or {"pending": 0, "pending_since": None, "last_completed_at": anchor}))
        stack.enter_context(patch.object(sched.db, "get_meta", return_value=None))
        return stack

    def test_disabled_blocks_everything(self):
        with self._patch(enabled=False):
            decision = sched.evaluate()
        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["state"], "disabled")

    def test_empty_pool_blocks(self):
        with self._patch(available=0):
            decision = sched.evaluate()
        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["state"], "pool_empty")

    def test_on_demand_pool_never_blocks_on_capacity(self):
        # available_emails 返回 -1 表示临时邮箱按需生成，不应被池空拦截。
        with self._patch(available=-1):
            decision = sched.evaluate()
        self.assertFalse(decision["blocked"])

    def test_active_job_blocks_to_keep_single_thread(self):
        with self._patch(job_counts={"active": 1, "running": 1}):
            decision = sched.evaluate()
        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["state"], "job_active")

    def test_pending_twofa_blocks(self):
        twofa = {"pending": 1, "pending_since": _ts(60), "last_completed_at": None}
        with self._patch(twofa=twofa):
            decision = sched.evaluate()
        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["state"], "twofa_pending")

    def test_pending_twofa_past_timeout_stops_waiting(self):
        # 2FA 卡死不能让自动注册永久停摆。
        twofa = {"pending": 1, "pending_since": _ts(99999), "last_completed_at": None}
        with self._patch(twofa=twofa):
            decision = sched.evaluate()
        self.assertFalse(decision["blocked"])

    def test_interval_not_elapsed_blocks(self):
        with self._patch(anchor=_ts(60)):
            decision = sched.evaluate()
        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["state"], "waiting")
        self.assertGreater(decision["wait_seconds"], 0)

    def test_interval_elapsed_allows(self):
        with self._patch(anchor=_ts(sched.interval_seconds() + 10)):
            decision = sched.evaluate()
        self.assertFalse(decision["blocked"])
        self.assertEqual(decision["state"], "ready")

    def test_no_history_fires_immediately(self):
        with self._patch(anchor=None):
            decision = sched.evaluate()
        self.assertFalse(decision["blocked"])

    def test_anchor_prefers_twofa_completion_over_job_completion(self):
        """间隔必须从 2FA 结束算，而不是注册任务结束。

        当前驱动不在注册流程里内联设置 2FA，任务结束时 2FA 往往还没开始。
        """
        twofa_done = _ts(120)
        with patch.object(sched.db, "get_meta", return_value=None), \
             patch.object(sched.db, "totp_setup_activity",
                          return_value={"pending": 0, "pending_since": None,
                                        "last_completed_at": twofa_done}):
            anchor = sched._anchor()
        self.assertEqual(anchor, datetime.fromisoformat(twofa_done))

    def test_stored_anchor_survives_restart(self):
        stored = _ts(300)
        with patch.object(sched.db, "get_meta", return_value=stored), \
             patch.object(sched.db, "totp_setup_activity",
                          return_value={"pending": 0, "pending_since": None,
                                        "last_completed_at": _ts(10)}):
            anchor = sched._anchor()
        self.assertEqual(anchor, datetime.fromisoformat(stored))


class AutoRegisterTickTests(unittest.TestCase):
    def test_tick_submits_one_job_single_thread(self):
        with patch.object(sched, "evaluate", return_value={"blocked": False, "state": "ready"}), \
             patch.object(sched, "_record_anchor") as record, \
             patch.object(sched.svc, "submit_registration",
                          return_value=[{"id": 42}]) as submit:
            result = sched._tick()
        submit.assert_called_once_with(count=1, workers=1)
        self.assertEqual(result["job_id"], 42)
        # 发起前先占锚点，否则快速失败的任务会让调度每轮都立刻重试。
        record.assert_called_once()

    def test_tick_does_not_submit_when_blocked(self):
        with patch.object(sched, "evaluate",
                          return_value={"blocked": True, "state": "waiting"}), \
             patch.object(sched.svc, "submit_registration") as submit:
            sched._tick()
        submit.assert_not_called()

    def test_submit_failure_keeps_anchor_so_it_backs_off(self):
        with patch.object(sched, "evaluate", return_value={"blocked": False, "state": "ready"}), \
             patch.object(sched, "_record_anchor") as record, \
             patch.object(sched.svc, "submit_registration",
                          side_effect=RuntimeError("池已关闭")):
            result = sched._tick()
        self.assertTrue(result["blocked"])
        self.assertEqual(result["state"], "submit_failed")
        record.assert_called_once()

    def test_note_twofa_finished_moves_anchor_forward(self):
        with patch.object(sched.db, "set_meta") as set_meta:
            sched.note_twofa_finished("a@b.com")
        set_meta.assert_called_once()
        key, value = set_meta.call_args[0]
        self.assertEqual(key, sched._ANCHOR_KEY)
        self.assertLess((datetime.now() - datetime.fromisoformat(value)).total_seconds(), 5)


class AutoRegisterPoolTests(unittest.TestCase):
    def test_on_demand_source_reports_unlimited(self):
        with patch.object(sched.db, "outlook_pool_summary", return_value={"available": 0}), \
             patch("core.email_provider.parse_email_sources", return_value=["mailnest"]):
            self.assertEqual(sched.available_emails(), -1)

    def test_outlook_pool_counts_available(self):
        with patch.object(sched.db, "outlook_pool_summary", return_value={"available": 7}), \
             patch("core.email_provider.parse_email_sources", return_value=["outlook"]):
            self.assertEqual(sched.available_emails(), 7)

    def test_multiple_local_pools_sum(self):
        with patch.object(sched.db, "outlook_pool_summary", return_value={"available": 2}), \
             patch.object(sched.db, "generic_api_email_pool_summary", return_value={"available": 3}), \
             patch("core.email_provider.parse_email_sources",
                   return_value=["outlook", "generic_api"]):
            self.assertEqual(sched.available_emails(), 5)


class AutoRegisterToggleTests(unittest.TestCase):
    def test_enabled_falls_back_to_config_when_unset(self):
        with patch.object(sched.db, "get_meta", return_value=None), \
             patch.object(sched._cfg, "AUTO_REGISTER_ENABLED", True):
            self.assertTrue(sched.is_enabled())

    def test_stored_switch_overrides_config(self):
        with patch.object(sched.db, "get_meta", return_value="0"), \
             patch.object(sched._cfg, "AUTO_REGISTER_ENABLED", True):
            self.assertFalse(sched.is_enabled())

    def test_disabled_tick_writes_nothing_and_submits_nothing(self):
        """关闭状态必须完全静默，不能留下锚点或发起注册。"""
        writes, calls = [], []
        with patch.object(sched.db, "get_meta", return_value=None), \
             patch.object(sched._cfg, "AUTO_REGISTER_ENABLED", False), \
             patch.object(sched.db, "set_meta", side_effect=lambda k, v: writes.append(k)), \
             patch.object(sched.svc, "submit_registration",
                          side_effect=lambda **kw: calls.append(kw)):
            decision = sched._tick()
        self.assertEqual(decision["state"], "disabled")
        self.assertEqual(writes, [])
        self.assertEqual(calls, [])

    def test_gate_order_blocks_on_job_before_checking_interval(self):
        """间隔已满也不能越过"有任务在跑"这道闸，闸门顺序是载荷。"""
        with patch.object(sched, "is_enabled", return_value=True), \
             patch.object(sched, "available_emails", return_value=5), \
             patch.object(sched.db, "job_status_counts", return_value={"active": 1}), \
             patch.object(sched.db, "get_meta", return_value=_ts(99999)), \
             patch.object(sched.db, "totp_setup_activity",
                          return_value={"pending": 0, "pending_since": None,
                                        "last_completed_at": None}):
            decision = sched.evaluate()
        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["state"], "job_active")


if __name__ == "__main__":
    unittest.main()
