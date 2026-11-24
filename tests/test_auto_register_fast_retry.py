# -*- coding: utf-8 -*-
"""自动注册：失败后快速换出口+换邮箱重试，不干等正常间隔。

需求：一次注册失败等半小时太浪费。失败往往是出口被挡、邮箱已废这类
换一组就能解决的问题，应该用短间隔立刻重试；但连续失败多次说明是
系统性故障（出口全废/邮箱池全坏），要回落到正常间隔等人介入。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
SCHED = (ROOT / "core" / "auto_register_scheduler.py").read_text(encoding="utf-8")
CFG = (ROOT / "config" / "auto_register.py").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = SCHED.index(f"def {name}(")
    nxt = SCHED.find("\ndef ", i + 1)
    return SCHED[i:nxt if nxt > 0 else len(SCHED)]


class ConfigTest(unittest.TestCase):
    def test_retry_interval_exists_and_short(self):
        m = re.search(r"AUTO_REGISTER_RETRY_INTERVAL\s*=\s*([\d.]+)", CFG)
        self.assertIsNotNone(m)
        # 要明显短于正常间隔（1800）才有意义
        self.assertLess(float(m.group(1)), 600)

    def test_max_fast_retries_exists(self):
        m = re.search(r"AUTO_REGISTER_MAX_FAST_RETRIES\s*=\s*(\d+)", CFG)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 1)

    def test_env_overridable(self):
        self.assertIn("'AUTO_REGISTER_RETRY_INTERVAL': 'float'", CFG)
        self.assertIn("'AUTO_REGISTER_MAX_FAST_RETRIES': 'int'", CFG)


class EffectiveIntervalTest(unittest.TestCase):
    def test_retry_interval_has_floor(self):
        """快速重试间隔要有下限，免得失败时疯狂空转。"""
        src = _fn("_retry_interval")
        self.assertIn("max(", src)

    def test_fast_window_bounded_by_max_retries(self):
        """只有 0<streak<=上限 才快速重试，超了回落正常间隔。"""
        src = _fn("_effective_interval")
        self.assertIn("_max_fast_retries()", src)
        self.assertIn("_retry_interval()", src)
        self.assertIn("interval_seconds()", src)

    def test_zero_streak_uses_normal(self):
        """没失败过（streak=0）必须走正常间隔。"""
        src = _fn("_effective_interval")
        self.assertIn("0 <", src)


class SettleLastJobTest(unittest.TestCase):
    def test_tick_settles_before_evaluate(self):
        """每轮先结算上一个任务，再判定间隔。"""
        src = _fn("_tick")
        self.assertLess(src.index("_settle_last_job()"),
                        src.index("evaluate()"))

    def test_records_job_id_after_submit(self):
        """发起后要记下 job_id，下轮才结算得了。"""
        src = _fn("_tick")
        self.assertIn("_LAST_JOB_KEY", src)

    def test_success_clears_streak(self):
        src = _fn("_settle_last_job")
        self.assertIn("_set_fail_streak(0)", src)

    def test_failure_bumps_streak(self):
        src = _fn("_settle_last_job")
        self.assertIn('"failed"', src)
        self.assertIn("streak", src.lower())

    def test_running_job_not_settled(self):
        """任务还在跑就别动计数，留到下轮。"""
        src = _fn("_settle_last_job")
        # queued/running 不在成功也不在失败分支里
        self.assertNotIn('"running"', src.split("success")[0]
                         if "success" in src else "")


class TwofaClearsStreakTest(unittest.TestCase):
    def test_twofa_finish_resets(self):
        """走到 2FA 说明注册成功，清零计数回到正常间隔。"""
        src = _fn("note_twofa_finished")
        self.assertIn("_set_fail_streak(0)", src)


class StatusExposesStreakTest(unittest.TestCase):
    def test_status_includes_fast_retry(self):
        src = _fn("status")
        self.assertIn("fast_retry", src)
        self.assertIn("fail_streak", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
