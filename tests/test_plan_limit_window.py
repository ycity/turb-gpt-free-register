# -*- coding: utf-8 -*-
"""套餐限额窗口要按窗口长度判定，不能按剩余时间推断。

事故：limit_windows 来自 conversation/init，按"距离下次重置还剩多少小时"
反推窗口类型。但剩余时间随查询时刻变化——weekly 窗口在后期查会被判成
daily 甚至 5h。而且那个接口返回的是聊天额度（deep_research/image_gen），
和 settings/usage 页上的「套餐限额」根本不是同一份数据。

正确做法：读 /backend-api/codex/usage 的 limit_window_seconds，
它是窗口长度本身，不随时间变化。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "core" / "chatgpt_plan.py").read_text(encoding="utf-8")


class WindowFromSecondsTest(unittest.TestCase):
    """按窗口长度判定——实测值 18000/86400/604800/2592000。"""

    def test_standard_windows(self):
        from core.chatgpt_plan import _window_from_seconds
        self.assertEqual(_window_from_seconds(18000), "5h")
        self.assertEqual(_window_from_seconds(86400), "daily")
        self.assertEqual(_window_from_seconds(604800), "weekly")
        self.assertEqual(_window_from_seconds(2592000), "monthly")

    def test_real_account_68_is_weekly(self):
        """#68 实测 limit_window_seconds=604800。"""
        from core.chatgpt_plan import _window_from_seconds
        self.assertEqual(_window_from_seconds(604800), "weekly")

    def test_tolerates_small_drift(self):
        from core.chatgpt_plan import _window_from_seconds
        self.assertEqual(_window_from_seconds(604800 * 1.05), "weekly")
        self.assertEqual(_window_from_seconds(86400 * 0.95), "daily")

    def test_refuses_to_guess_unknown_window(self):
        """没见过的窗口宁可留空，不要硬套最近的。"""
        from core.chatgpt_plan import _window_from_seconds
        self.assertEqual(_window_from_seconds(200000), "")

    def test_rejects_garbage(self):
        from core.chatgpt_plan import _window_from_seconds
        for bad in (0, -1, None, "", "abc"):
            self.assertEqual(_window_from_seconds(bad), "")

    def test_does_not_drift_with_elapsed_time(self):
        """同一个窗口无论查询时刻，判定结果不变——这正是旧实现的缺陷。"""
        from core.chatgpt_plan import _window_from_seconds
        for _ in range(3):
            self.assertEqual(_window_from_seconds(604800), "weekly")


class OldInferenceIsDocumentedAsUnreliableTest(unittest.TestCase):
    def test_still_exists_for_fallback(self):
        """聊天额度那条路没有窗口长度，仍需推断，但要标注不可靠。"""
        from core.chatgpt_plan import _limit_window
        self.assertEqual(_limit_window(5.0), "5h")

    def test_flaw_is_documented(self):
        idx = SRC.index("def _limit_window")
        body = SRC[idx:idx + 700]
        self.assertIn("推断", body)
        self.assertIn("随查询时刻变化", body)

    def test_known_misjudgement_is_real(self):
        """记录缺陷本身：weekly 窗口剩 5 小时会被判成 5h。"""
        from core.chatgpt_plan import _limit_window
        self.assertEqual(_limit_window(5.0), "5h")   # 真实窗口可能是 weekly


class CodexUsageFetchTest(unittest.TestCase):
    class FakeResp:
        def __init__(self, payload, status=200):
            self._p = payload
            self.status_code = status

        def json(self):
            return self._p

    class FakeEnv:
        def __init__(self, payload, status=200):
            self._p = payload
            self._s = status
            self.last_headers = None
            self.last_url = None

        def get_chatgpt_headers(self, referer=""):
            return {"referer": referer}

        def get(self, url, headers=None, **kw):
            self.last_url = url
            self.last_headers = headers
            return CodexUsageFetchTest.FakeResp(self._p, self._s)

    WEEKLY = {
        "plan_type": "team",
        "rate_limit": {
            "limit_reached": False,
            "primary_window": {"used_percent": 0,
                               "limit_window_seconds": 604800,
                               "reset_at": 1794470747},
            "secondary_window": None,
        },
    }
    FIVE_H_PLUS_WEEKLY = {
        "rate_limit": {
            "limit_reached": False,
            "primary_window": {"used_percent": 12,
                               "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 3,
                                 "limit_window_seconds": 604800},
        },
    }

    def test_weekly_only(self):
        from core.chatgpt_plan import fetch_codex_usage
        env = self.FakeEnv(self.WEEKLY)
        out = fetch_codex_usage(env, "tok", account_id="ws-1")
        self.assertTrue(out["plan_limit_ok"])
        self.assertEqual(out["plan_limit_windows"], ["weekly"])

    def test_both_windows_sorted_short_first(self):
        from core.chatgpt_plan import fetch_codex_usage
        out = fetch_codex_usage(self.FakeEnv(self.FIVE_H_PLUS_WEEKLY), "tok")
        self.assertEqual(out["plan_limit_windows"], ["5h", "weekly"])

    def test_keeps_usage_detail(self):
        from core.chatgpt_plan import fetch_codex_usage
        out = fetch_codex_usage(self.FakeEnv(self.WEEKLY), "tok")
        d = out["plan_limit_detail"][0]
        self.assertEqual(d["window"], "weekly")
        self.assertEqual(d["used_percent"], 0)

    def test_sends_workspace_header(self):
        """必须带 chatgpt-account-id，否则读到的是别的空间。"""
        from core.chatgpt_plan import fetch_codex_usage
        env = self.FakeEnv(self.WEEKLY)
        fetch_codex_usage(env, "tok", account_id="ws-42")
        self.assertEqual(env.last_headers.get("chatgpt-account-id"), "ws-42")

    def test_uses_codex_usage_endpoint(self):
        from core.chatgpt_plan import fetch_codex_usage
        env = self.FakeEnv(self.WEEKLY)
        fetch_codex_usage(env, "tok")
        self.assertIn("/backend-api/codex/usage", env.last_url)

    def test_http_error_is_not_fatal(self):
        from core.chatgpt_plan import fetch_codex_usage
        out = fetch_codex_usage(self.FakeEnv({}, status=403), "tok")
        self.assertFalse(out["plan_limit_ok"])
        self.assertIn("403", out["plan_limit_error"])

    def test_null_rate_limit(self):
        from core.chatgpt_plan import fetch_codex_usage
        out = fetch_codex_usage(self.FakeEnv({"rate_limit": None}), "tok")
        self.assertTrue(out["plan_limit_ok"])
        self.assertEqual(out["plan_limit_windows"], [])


class WiringTest(unittest.TestCase):
    def test_plan_check_calls_codex_usage(self):
        self.assertIn("fetch_codex_usage(", SRC)

    def test_runs_only_for_paid_accounts(self):
        # rindex 取调用处，index 会落在函数定义上
        idx = SRC.rindex("parsed.update(fetch_codex_usage(")
        window = SRC[max(0, idx - 2000):idx]
        self.assertIn("has_active_subscription", window)

    def test_failure_does_not_break_plan_result(self):
        idx = SRC.rindex("parsed.update(fetch_codex_usage(")
        window = SRC[idx:idx + 700]
        self.assertIn("except Exception", window)
        self.assertIn("plan_limit_ok", window)

    def test_fields_are_persisted(self):
        db = (ROOT / "core" / "db.py").read_text(encoding="utf-8")
        for f in ("plan_limit_windows", "plan_limit_detail",
                  "plan_limit_checked_at"):
            self.assertIn(f, db, f"{f} 没有落库")


if __name__ == "__main__":
    unittest.main(verbosity=2)
