import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from core.chatgpt_plan import _limit_window, fetch_limit_windows


def iso_in(hours):
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).isoformat()


def fake_env(payload, status=200):
    env = MagicMock()
    env.get_chatgpt_headers.return_value = {}
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    env.post.return_value = resp
    return env


class LimitWindowClassifyTests(unittest.TestCase):
    def test_observed_intervals_map_to_expected_windows(self):
        # 真实 Team 账号实测间隔：reason 5.00h，file_upload 24.00h，deep_research 720h。
        self.assertEqual(_limit_window(5.0), "5h")
        self.assertEqual(_limit_window(24.0), "daily")
        self.assertEqual(_limit_window(168.0), "weekly")
        self.assertEqual(_limit_window(720.0), "monthly")

    def test_boundaries_and_non_positive(self):
        self.assertEqual(_limit_window(0.1), "5h")
        self.assertEqual(_limit_window(6.0), "5h")
        self.assertEqual(_limit_window(6.1), "daily")
        self.assertEqual(_limit_window(36.0), "daily")
        self.assertEqual(_limit_window(36.1), "weekly")
        self.assertEqual(_limit_window(200.0), "weekly")
        self.assertEqual(_limit_window(200.1), "monthly")
        self.assertEqual(_limit_window(0), "")
        self.assertEqual(_limit_window(-3), "")


class FetchLimitWindowsTests(unittest.TestCase):
    def test_parses_real_shaped_response(self):
        env = fake_env({
            "limits_progress": [
                {"feature_name": "deep_research", "remaining": 5, "reset_after": iso_in(720)},
                {"feature_name": "file_upload", "remaining": 15, "reset_after": iso_in(24)},
                {"feature_name": "reason", "remaining": 50, "reset_after": iso_in(5)},
            ],
            "blocked_features": [],
        })
        out = fetch_limit_windows(env, "tok", {"account_id": "p"})
        self.assertTrue(out["limit_check_ok"])
        # 窗口按时长排序，5h 在最前。
        self.assertEqual(out["limit_windows"], ["5h", "daily", "monthly"])
        by_feature = {f["feature"]: f["window"] for f in out["limit_features"]}
        self.assertEqual(by_feature["reason"], "5h")
        self.assertEqual(by_feature["deep_research"], "monthly")

    def test_remaining_counts_are_not_stored(self):
        env = fake_env({"limits_progress": [
            {"feature_name": "reason", "remaining": 42, "reset_after": iso_in(5)}]})
        out = fetch_limit_windows(env, "tok", {"account_id": "p"})
        self.assertNotIn("42", json.dumps(out))
        for f in out["limit_features"]:
            self.assertNotIn("remaining", f)

    def test_blocked_features_contribute_their_window(self):
        env = fake_env({
            "limits_progress": [],
            "blocked_features": [
                {"name": "image_gen", "resets_after": iso_in(168), "limit": 0.0}],
        })
        out = fetch_limit_windows(env, "tok", {"account_id": "p"})
        self.assertEqual(out["limit_windows"], ["weekly"])
        self.assertEqual(out["limit_features"][0]["feature"], "image_gen")
        self.assertTrue(out["limit_features"][0]["blocked"])

    def test_empty_limits_yield_no_windows(self):
        env = fake_env({"limits_progress": [], "blocked_features": []})
        out = fetch_limit_windows(env, "tok", {"account_id": "p"})
        self.assertTrue(out["limit_check_ok"])
        self.assertEqual(out["limit_windows"], [])

    def test_malformed_entries_are_skipped(self):
        env = fake_env({"limits_progress": [
            {"feature_name": "ok", "reset_after": iso_in(5)},
            {"feature_name": "no_reset"},
            {"reset_after": iso_in(5)},
            {"feature_name": "bad_ts", "reset_after": "not-a-date"},
            "junk",
        ]})
        out = fetch_limit_windows(env, "tok", {"account_id": "p"})
        self.assertEqual([f["feature"] for f in out["limit_features"]], ["ok"])

    def test_http_error_reports_failure_without_raising(self):
        env = fake_env({}, status=403)
        out = fetch_limit_windows(env, "tok", {"account_id": "p"})
        self.assertFalse(out["limit_check_ok"])
        self.assertIn("403", out["limit_check_error"])

    def test_team_workspace_id_is_sent(self):
        env = fake_env({"limits_progress": []})
        fetch_limit_windows(env, "tok", {"account_id": "personal"}, account_id="team-ws")
        headers = env.post.call_args.kwargs["headers"]
        self.assertEqual(headers["chatgpt-account-id"], "team-ws")


if __name__ == "__main__":
    unittest.main()
