# -*- coding: utf-8 -*-
"""查活默认不切换代理。

换 IP 换指纹反复登录同一账号本身就像盗号，而且换出口后的 403 会盖掉真实
状态——实测同一账号在注册代理上返回 account_deactivated，批量并发换出口后
只剩一个看不出所以然的 403。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import live_check_service as live  # noqa: E402


class _DummySlot:
    """_run_live_check 只会 release，不该用真信号量。"""

    def release(self):
        pass

FAILED = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
SUCCESS = {"ok": True, "status": "live", "access_token": "tok"}
PA, PB = "socks5://a.example:1080", "socks5://b.example:1080"


class NoRotateByDefaultTest(unittest.TestCase):
    def _run(self, switch, side_effect):
        calls = []

        def spy(email, **kw):
            calls.append(kw.get("proxy"))
            return side_effect[len(calls) - 1]

        with patch.object(live, "_QUEUE_SLOTS", _DummySlot()), \
             patch.object(live.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live.db, "get_account",
                          return_value={"email": "u@e.test", "email_source": "outlook"}), \
             patch.object(live.db, "update_account_liveness"), \
             patch.object(live, "_append_log"), \
             patch.object(live, "_configured_live_check_proxies", return_value=[PA, PB]), \
             patch.object(live, "resolve_plan_check_route", return_value={"proxy": PA}), \
             patch.object(live, "_live_proxy_route", return_value={"proxy": PB}), \
             patch.object(live, "open_plan_check_proxy", return_value=(None, None)), \
             patch("config.proxy.LIVE_CHECK_SWITCH_PROXY_ON_FAILURE", switch, create=True), \
             patch.object(live, "check_account_liveness", side_effect=spy):
            res = live._run_live_check(account_id=1, email="u@e.test",
                                       proxy=None, trigger="manual")
        return res, calls

    def test_default_tries_once_only(self):
        res, calls = self._run(False, [FAILED, SUCCESS])
        self.assertEqual(len(calls), 1, "默认不该换出口重试")
        self.assertFalse(res["ok"])

    def test_opt_in_still_rotates(self):
        res, calls = self._run(True, [FAILED, SUCCESS])
        self.assertEqual(len(calls), 2, "开启后应换出口重试一次")
        self.assertTrue(res["ok"])

    def test_success_never_retries(self):
        _, calls = self._run(False, [SUCCESS])
        self.assertEqual(len(calls), 1)

    def test_deactivated_is_not_retried_even_when_enabled(self):
        """明确的封号判定不是网络问题，不该再换出口。"""
        dead = {"ok": False, "status": "deactivated", "error": "account_deactivated"}
        _, calls = self._run(True, [dead, SUCCESS])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
