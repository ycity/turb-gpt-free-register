# -*- coding: utf-8 -*-
import importlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import env_loader
from config import proxy as proxy_cfg
from config.proxy import format_proxy_pool_line, proxy_pool_entries, split_proxy_pool_line
from core import proxy_probe
from webui import proxy_pool
from webui.app import create_app


class ProxyPoolLineFormatTests(unittest.TestCase):
    def test_split_reads_name_and_disabled_marker(self):
        self.assertEqual(
            split_proxy_pool_line("socks5h://u:p@1.2.3.4:1080#美国 #1"),
            ("socks5h://u:p@1.2.3.4:1080", "美国 #1", True),
        )
        self.assertEqual(
            split_proxy_pool_line("# http://5.6.7.8:8080#备用"),
            ("http://5.6.7.8:8080", "备用", False),
        )
        self.assertEqual(split_proxy_pool_line("http://5.6.7.8:8080"), ("http://5.6.7.8:8080", "", True))

    def test_shorthand_keeps_hash_inside_password(self):
        self.assertEqual(split_proxy_pool_line("1.1.1.1:3128:user:pa#ss"), ("1.1.1.1:3128:user:pa#ss", "", True))
        self.assertEqual(proxy_pool_entries(["1.1.1.1:3128:user:pa#ss"])[0]["url"], "http://user:pa%23ss@1.1.1.1:3128")

    def test_format_round_trips_through_split(self):
        line = format_proxy_pool_line("socks5h://u:p@1.2.3.4:1080", "  美国\n家宽 ", enabled=False)
        self.assertEqual(line, "# socks5h://u:p@1.2.3.4:1080#美国 家宽")
        self.assertEqual(split_proxy_pool_line(line), ("socks5h://u:p@1.2.3.4:1080", "美国 家宽", False))

    def test_unbalanced_ipv6_lines_do_not_raise(self):
        entries = proxy_pool_entries(["# [::1:8080", "[::1:8080", "http://1.2.3.4:80"])
        self.assertEqual([entry["enabled"] for entry in entries], [False, True, True])
        self.assertEqual(entries[2]["url"], "http://1.2.3.4:80")

    def test_format_rejects_address_without_scheme_or_port(self):
        with self.assertRaises(ValueError):
            format_proxy_pool_line("not a proxy", "名称")

    def test_runtime_pool_keeps_enabled_urls_without_names(self):
        old_loaded = env_loader._LOADED
        pool = "socks5h://u:p@1.2.3.4:1080#美国\n# http://5.6.7.8:8080#备用\n#\n9.9.9.9:3128"
        try:
            env_loader._LOADED = True
            with patch.dict(os.environ, {"PROXY_POOL": pool}):
                importlib.reload(proxy_cfg)
                self.assertEqual(proxy_cfg.PROXY_POOL, ["socks5h://u:p@1.2.3.4:1080", "http://9.9.9.9:3128"])
                self.assertIn(proxy_cfg.pick_proxy(), proxy_cfg.PROXY_POOL)
        finally:
            env_loader._LOADED = old_loaded
            importlib.reload(proxy_cfg)


class _IsolatedPoolMixin:
    """把 .env 和测试结果库重定向到临时目录，并跳过整包配置热加载。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for patcher in (
            patch.dict(os.environ),
            patch.object(env_loader, "_ENV_PATH", root / ".env"),
            patch.object(proxy_pool, "DB_PATH", root / "proxy-tests.sqlite3"),
            patch("config.reload_all"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self, *lines):
        env_loader.write_env_values({"PROXY_POOL": "\n".join(lines)})

    def env_lines(self):
        return env_loader.read_env_file()["PROXY_POOL"].splitlines()


class ProxyPoolServiceTests(_IsolatedPoolMixin, unittest.TestCase):
    def test_list_describes_names_disabled_and_invalid_lines(self):
        self.seed("socks5h://u:p@1.2.3.4:1080#美国", "# http://5.6.7.8:8080#备用", "1.2.3.4")

        items = proxy_pool.list_pool()["items"]

        self.assertEqual([(i["name"], i["enabled"], i["valid"]) for i in items],
                         [("美国", True, True), ("备用", False, True), ("", True, False)])
        self.assertEqual((items[0]["scheme"], items[0]["host"], items[0]["port"], items[0]["username"]),
                         ("socks5h", "1.2.3.4", 1080, "u"))
        self.assertTrue(items[2]["error"])

    def test_list_survives_unparseable_ipv6_line(self):
        self.seed("# [::1:8080", "http://5.6.7.8:8080#可用")

        items = proxy_pool.list_pool()["items"]

        self.assertEqual([(i["valid"], i["enabled"]) for i in items], [(False, False), (True, True)])

    def test_concurrent_env_writes_keep_every_key(self):
        def write(n):
            for i in range(15):
                env_loader.write_env_values({f"POOL_TEST_KEY_{n}": str(i)})

        threads = [threading.Thread(target=write, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        values = env_loader.read_env_file()
        self.assertEqual({k: values.get(k) for k in (f"POOL_TEST_KEY_{n}" for n in range(6))},
                         {f"POOL_TEST_KEY_{n}": "14" for n in range(6)})

    def test_save_keeps_untouched_lines_verbatim(self):
        self.seed("1.1.1.1:3128:user:pa#ss", "# 只是注释", "http://5.6.7.8:8080#旧名称")
        listing = proxy_pool.list_pool()
        items = [{"raw": item["raw"]} for item in listing["items"]]
        items[2] = {"name": "新名称", "url": "HTTP://5.6.7.8:8080/", "enabled": False}

        proxy_pool.save_pool(items, version=listing["version"])

        self.assertEqual(self.env_lines(), ["1.1.1.1:3128:user:pa#ss", "# 只是注释", "# http://5.6.7.8:8080#新名称"])

    def test_toggle_only_flips_marker_even_for_duplicate_lines(self):
        self.seed("http://5.6.7.8:8080#甲", "http://5.6.7.8:8080#乙")
        listing = proxy_pool.list_pool()

        proxy_pool.save_pool([{"raw": listing["items"][0]["raw"], "enabled": False},
                              {"raw": listing["items"][1]["raw"]}], version=listing["version"])

        self.assertEqual(self.env_lines(), ["# http://5.6.7.8:8080#甲", "http://5.6.7.8:8080#乙"])

    def test_save_rejects_address_that_already_exists(self):
        self.seed("socks5h://u:p@1.2.3.4:1080#美国")
        listing = proxy_pool.list_pool()

        with self.assertRaisesRegex(ValueError, "与第 1 个代理「美国」地址相同"):
            proxy_pool.save_pool([{"raw": listing["items"][0]["raw"]},
                                  {"name": "", "url": "SOCKS5H://u:p@1.2.3.4:1080", "enabled": True}],
                                 version=listing["version"])

    def test_save_rejects_stale_version(self):
        self.seed("http://5.6.7.8:8080")
        version = proxy_pool.list_pool()["version"]
        self.seed("http://5.6.7.8:8080", "http://9.9.9.9:8080#别处新增")

        with self.assertRaises(proxy_pool.ProxyPoolConflict):
            proxy_pool.save_pool([{"raw": "http://5.6.7.8:8080"}], version=version)
        self.assertEqual(len(self.env_lines()), 2)

    def test_import_dry_run_does_not_write(self):
        self.seed("http://5.6.7.8:8080")

        result = proxy_pool.import_lines("9.9.9.9:3128\nbad line", dry_run=True)

        self.assertEqual((result["added"], result["duplicates"], len(result["invalid"])), (1, 0, 1))
        self.assertEqual(self.env_lines(), ["http://5.6.7.8:8080"])

    def test_import_names_with_prefix_and_skips_equivalent_addresses(self):
        self.seed("http://5.6.7.8:8080#家宽 2")
        text = "\n".join([
            "http://5.6.7.8:8080/",
            "HTTP://5.6.7.8:8080",
            "9.9.9.9:3128:user:pass",
            "socks5h://a:b@3.3.3.3:1080#日本",
            "9.9.9.9:3128:user:pass",
        ])

        result = proxy_pool.import_lines(text, default_scheme="socks5h", name_prefix="家宽")

        self.assertEqual((result["added"], result["duplicates"]), (2, 3))
        self.assertEqual(self.env_lines(), [
            "http://5.6.7.8:8080#家宽 2",
            "socks5h://user:pass@9.9.9.9:3128#家宽 3",
            "socks5h://a:b@3.3.3.3:1080#日本",
        ])
        self.assertEqual(len(result["added_keys"]), 2)

    def test_test_result_follows_equivalent_address_and_is_pruned_on_delete(self):
        self.seed("http://5.6.7.8:8080/#甲", "http://9.9.9.9:8080#乙")
        fake = {"ok": True, "latency_ms": 321, "ip": "8.8.8.8", "message": "连接正常", "tested_at": "2026-10-31T18:00:00+08:00"}
        with patch("core.proxy_probe.probe_proxy", return_value=fake) as probe:
            checked = proxy_pool.check_proxy("HTTP://5.6.7.8:8080")
        probe.assert_called_once()

        listing = proxy_pool.list_pool()
        self.assertEqual(listing["items"][0]["key"], checked["key"])
        self.assertEqual(listing["items"][0]["test"]["latency_ms"], 321)

        proxy_pool.save_pool([{"raw": listing["items"][1]["raw"]}], version=listing["version"])
        self.assertEqual(proxy_pool._load_results(), {})

    def test_check_rejects_unparseable_address(self):
        with self.assertRaisesRegex(ValueError, "无法识别的代理格式"):
            proxy_pool.check_proxy("not a proxy")


class ProxyPoolApiTests(_IsolatedPoolMixin, unittest.TestCase):
    HEADERS = {"X-Auth-Code": "test-auth", "Accept-Encoding": "identity"}

    def test_routes_list_save_import_and_test(self):
        self.seed("http://5.6.7.8:8080#甲")
        client = create_app(auth_code="test-auth").test_client()

        listing = client.get("/api/proxy-pool", headers=self.HEADERS).get_json()
        self.assertEqual([item["name"] for item in listing["items"]], ["甲"])

        stale = client.post("/api/proxy-pool", headers=self.HEADERS,
                            json={"version": "stale", "items": []})
        self.assertEqual(stale.status_code, 409)

        bad = client.post("/api/proxy-pool", headers=self.HEADERS, json={
            "version": listing["version"], "items": [{"name": "x", "url": "nope", "enabled": True}],
        })
        self.assertEqual(bad.status_code, 400)

        imported = client.post("/api/proxy-pool/import", headers=self.HEADERS, json={
            "version": listing["version"], "text": "socks5h://9.9.9.9:1080#乙",
        }).get_json()
        self.assertEqual([item["name"] for item in imported["items"]], ["甲", "乙"])

        with patch("core.proxy_probe.probe_proxy", return_value={"ok": False, "message": "连接超时"}):
            tested = client.post("/api/proxy-pool/test", headers=self.HEADERS,
                                 json={"url": "socks5h://9.9.9.9:1080"}).get_json()
        self.assertEqual(tested["result"]["message"], "连接超时")
        self.assertEqual(client.post("/api/proxy-pool/test", headers=self.HEADERS, json={"url": ""}).status_code, 400)

    def test_routes_require_auth(self):
        client = create_app(auth_code="test-auth").test_client()
        self.assertEqual(client.get("/api/proxy-pool", headers={"Accept-Encoding": "identity"}).status_code, 401)


class _FakeResponse:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


class _CurlError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class ProxyProbeTests(unittest.TestCase):
    def _session(self, responses):
        session = MagicMock()
        session.get.side_effect = lambda url, **kwargs: responses(url)
        return session

    def test_success_reports_trace_exit_and_geo_of_that_ip(self):
        def responses(url):
            if url == proxy_probe.TRACE_URL:
                return _FakeResponse(text="fl=1\nip=203.0.113.9\ncolo=LAX\nloc=US\n")
            self.assertIn("203.0.113.9", url)
            return _FakeResponse(payload={"ip": "203.0.113.9", "city": "Los Angeles", "region": "California",
                                          "country": "US", "org": "AS15169 Google LLC"})
        session = self._session(responses)
        with patch("curl_cffi.requests.Session", return_value=session), \
                patch("core.proxy_chain.open_proxy_pool_proxy", return_value=("http://1.2.3.4:8080", None)):
            result = proxy_probe.probe_proxy("http://1.2.3.4:8080")

        self.assertTrue(result["ok"])
        self.assertEqual((result["ip"], result["country"], result["colo"], result["city"]),
                         ("203.0.113.9", "US", "LAX", "Los Angeles"))
        self.assertEqual(result["datacenter"], "google llc")
        self.assertIsInstance(result["latency_ms"], int)
        session.close.assert_called_once()

    def test_proxy_auth_failure_is_explained(self):
        error = _CurlError("Failed to perform, curl: (7) CONNECT tunnel failed, response 407. "
                           "See https://curl.se/libcurl/c/libcurl-errors.html first for more details.", 7)
        session = MagicMock()
        session.get.side_effect = error
        with patch("curl_cffi.requests.Session", return_value=session), \
                patch("core.proxy_chain.open_proxy_pool_proxy", return_value=("http://u:x@1.2.3.4:8080", None)):
            result = proxy_probe.probe_proxy("http://u:x@1.2.3.4:8080")

        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "代理认证失败（407），请检查用户名和密码")
        self.assertEqual(result["detail"], "curl: (7) CONNECT tunnel failed, response 407.")

    def test_upstream_chain_is_used_and_closed(self):
        relay = MagicMock()
        session = self._session(lambda url: _FakeResponse(status_code=403))
        with patch("curl_cffi.requests.Session", return_value=session), \
                patch("core.proxy_chain.open_proxy_pool_proxy", return_value=("http://127.0.0.1:50000", relay)):
            result = proxy_probe.probe_proxy("socks5h://1.2.3.4:1080")

        self.assertTrue(result["via_upstream"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "chatgpt.com 返回 HTTP 403")
        self.assertEqual(session.proxies, {"http": "http://127.0.0.1:50000", "https": "http://127.0.0.1:50000"})
        relay.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
