import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import team_checkout_service as svc
from webui import proxy_pool

PROXY = "socks5h://u:p@1.2.3.4:1080"
OTHER = "http://9.9.9.9:8080"


class TestedGeoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "p.sqlite3"
        p = patch.object(proxy_pool, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)

    def save(self, url, result):
        # Windows 下未关闭的连接会让 TemporaryDirectory 清理失败。
        conn = sqlite3.connect(str(self.db))
        try:
            with conn:
                conn.execute(proxy_pool._SCHEMA)
                conn.execute(
                    "INSERT OR REPLACE INTO proxy_pool_tests"
                    " (proxy_key, result_json, tested_at) VALUES (?,?,?)",
                    (proxy_pool.proxy_key(url), json.dumps(result),
                     "2026-11-01T00:00:00"))
        finally:
            conn.close()

    def test_returns_country_for_tested_proxy(self):
        self.save(PROXY, {"ok": True, "country": "SG", "city": "Singapore",
                          "ip": "1.2.3.4", "region": "SG", "org": "Example"})
        geo = proxy_pool.tested_geo(PROXY)
        self.assertEqual(geo["country"], "SG")
        self.assertEqual(geo["city"], "Singapore")

    def test_untested_proxy_returns_empty(self):
        self.assertEqual(proxy_pool.tested_geo(PROXY), {})

    def test_failed_test_returns_empty(self):
        self.save(PROXY, {"ok": False, "country": "SG"})
        self.assertEqual(proxy_pool.tested_geo(PROXY), {})

    def test_missing_country_returns_empty(self):
        self.save(PROXY, {"ok": True, "country": "", "city": "Nowhere"})
        self.assertEqual(proxy_pool.tested_geo(PROXY), {})

    def test_blank_and_malformed_input(self):
        self.assertEqual(proxy_pool.tested_geo(""), {})
        self.assertEqual(proxy_pool.tested_geo(None), {})
        self.assertEqual(proxy_pool.tested_geo("not a proxy"), {})

    def test_shorthand_matches_canonical_entry(self):
        self.save("http://10.0.0.1:8080", {"ok": True, "country": "US"})
        # 代理池按规范化地址做 key，简写也应该命中同一条记录。
        self.assertEqual(proxy_pool.tested_geo("http://10.0.0.1:8080")["country"], "US")


class RegistrationExitGeoTests(unittest.TestCase):
    def test_prefers_current_proxy(self):
        with patch.object(proxy_pool, "tested_geo",
                          side_effect=lambda u: {"country": "SG"} if u == PROXY else {"country": "JP"}):
            self.assertEqual(svc._registration_exit_geo(PROXY, OTHER)["country"], "SG")

    def test_falls_back_to_account_proxy(self):
        with patch.object(proxy_pool, "tested_geo",
                          side_effect=lambda u: {"country": "US"} if u == OTHER else {}):
            self.assertEqual(svc._registration_exit_geo(PROXY, OTHER)["country"], "US")

    def test_empty_when_neither_tested(self):
        with patch.object(proxy_pool, "tested_geo", return_value={}):
            self.assertEqual(svc._registration_exit_geo(PROXY, OTHER), {})

    def test_blank_candidates_are_skipped(self):
        calls = []

        def spy(u):
            calls.append(u)
            return {"country": "US"}

        with patch.object(proxy_pool, "tested_geo", side_effect=spy):
            svc._registration_exit_geo("", OTHER)
        self.assertEqual(calls, [OTHER])

    def test_lookup_failure_does_not_raise(self):
        with patch.object(proxy_pool, "tested_geo", side_effect=RuntimeError("db gone")):
            self.assertEqual(svc._registration_exit_geo(PROXY, OTHER), {})


if __name__ == "__main__":
    unittest.main()
