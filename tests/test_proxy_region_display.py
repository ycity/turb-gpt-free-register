# -*- coding: utf-8 -*-
"""注册出口地区要记录下来，并显示在「注册代理」列。

背景（2026-11-17）：
  切到 1024proxy 后，出口是现取的临时 IP，不在代理池里。界面那列按
  地址反查代理池名称，查不到就显示 "-"——#103~#106 全是 "-"，
  事后分不清哪个号用了哪个地区，没法做出口维度的存活分析。

  而这恰恰是今天最需要的数据：旧代理池 100% 死、1024proxy 25% 死，
  差异就在出口上。

修法：取出口时把实际探到的国家一路带下来，存进 proxy_region。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class AcquireReturnsRegionTest(unittest.TestCase):
    def test_returns_tuple(self):
        """取出口要同时给出地区，否则没法记录。"""
        from config import register_proxy as cfg
        from core import cloakbrowser_driver as drv
        with patch.object(cfg, "REGISTER_PROXY_USE_1024", True), \
             patch("core.register_proxy.acquire_register_proxy",
                   return_value={"ok": True, "proxy": "http://1.2.3.4:9",
                                 "country": "US", "region": "US"}), \
             patch("config.proxy.pick_proxy", return_value=""):
            got = drv._acquire_registration_exit()
        self.assertIsInstance(got, tuple)
        self.assertEqual(got[0], "http://1.2.3.4:9")
        self.assertEqual(got[1], "1024·US")

    def test_uses_actual_country_not_requested(self):
        """1024proxy 偶尔给到别的国家，要记实际出口。"""
        from config import register_proxy as cfg
        from core import cloakbrowser_driver as drv
        with patch.object(cfg, "REGISTER_PROXY_USE_1024", True), \
             patch("core.register_proxy.acquire_register_proxy",
                   return_value={"ok": True, "proxy": "http://x:1",
                                 "country": "JP", "region": "US"}), \
             patch("config.proxy.pick_proxy", return_value=""):
            _, region = drv._acquire_registration_exit()
        self.assertEqual(region, "1024·JP", "应记实际出口国家，不是请求的")

    def test_pool_fallback_has_empty_region(self):
        """回落代理池时地区留空，由代理池名称顶上。"""
        from config import register_proxy as cfg
        from core import cloakbrowser_driver as drv
        with patch.object(cfg, "REGISTER_PROXY_USE_1024", False), \
             patch("config.proxy.pick_proxy", return_value="http://pool:1"):
            proxy, region = drv._acquire_registration_exit()
        self.assertEqual(proxy, "http://pool:1")
        self.assertEqual(region, "")

    def test_region_missing_still_labelled(self):
        """拿不到国家时也要有个标签，别又变成 '-'。"""
        from config import register_proxy as cfg
        from core import cloakbrowser_driver as drv
        with patch.object(cfg, "REGISTER_PROXY_USE_1024", True), \
             patch("core.register_proxy.acquire_register_proxy",
                   return_value={"ok": True, "proxy": "http://x:1"}), \
             patch("config.proxy.pick_proxy", return_value=""):
            _, region = drv._acquire_registration_exit()
        self.assertTrue(region, "至少要标出是 1024proxy")


class PipelineTest(unittest.TestCase):
    """地区要一路传到账号记录里。"""

    def test_driver_puts_region_in_raw(self):
        import inspect
        from core import cloakbrowser_driver as drv
        src = inspect.getsource(drv.build_cloak_driver)
        self.assertIn('"proxy_region": proxy_region', src)

    def test_registration_passes_region(self):
        src = (ROOT / "core" / "cloakbrowser_registration.py").read_text(
            encoding="utf-8")
        self.assertIn("proxy_region=", src)
        self.assertIn('"proxy_region"', src)

    def test_save_account_accepts_region(self):
        import inspect
        from core import account_export
        sig = inspect.signature(account_export.save_account_data)
        self.assertIn("proxy_region", sig.parameters)

    def test_save_account_forwards_region(self):
        import inspect
        from core import account_export
        src = inspect.getsource(account_export.save_account_data)
        self.assertIn("proxy_region=proxy_region", src)

    def test_db_accepts_and_stores_region(self):
        import inspect
        from core import db
        sig = inspect.signature(db.insert_account)
        self.assertIn("proxy_region", sig.parameters)
        src = inspect.getsource(db.insert_account)
        self.assertIn('"proxy_region"', src)

    def test_db_keeps_existing_region_when_blank(self):
        """补写账号时不能用空值把已有地区冲掉。"""
        import inspect
        from core import db
        src = inspect.getsource(db.insert_account)
        self.assertIn('proxy_region if proxy_region else row.get("proxy_region")',
                      src)


class DisplayTest(unittest.TestCase):
    def test_region_preferred_over_pool_name(self):
        """没命中池内命名条目时，仍用注册地区（1024 临时出口查不到名）。"""
        from webui.app import _registration_proxy_name
        row = {"proxy_region": "1024·US", "proxy_used": "http://1.2.3.4:9"}
        self.assertEqual(_registration_proxy_name(row, {}), "1024·US")

    def test_pool_name_wins_over_region_after_adopt(self):
        """adopt_pool_exit 固化成池出口后：proxy_used 命中命名条目，
        应显示真名（Zoro-SG），而不是泛化的「池·SG」。"""
        from webui.app import _proxy_identity, _registration_proxy_name
        url = "http://u:p@61.13.236.169:6611"
        names = {_proxy_identity(url): "Zoro-SG"}
        row = {"proxy_region": "池·SG", "proxy_used": url}
        self.assertEqual(_registration_proxy_name(row, names), "Zoro-SG")

    def test_falls_back_to_pool_name(self):
        """旧账号没有 proxy_region，仍按代理池名称显示。"""
        from webui.app import _proxy_identity, _registration_proxy_name
        url = "http://u:p@host:6611"
        names = {_proxy_identity(url): "Zoro-SG"}
        row = {"proxy_used": url}
        self.assertEqual(_registration_proxy_name(row, names), "Zoro-SG")

    def test_no_region_no_name_is_blank(self):
        from webui.app import _registration_proxy_name
        row = {"proxy_used": "http://1.2.3.4:9"}
        self.assertEqual(_registration_proxy_name(row, {}), "")

    def test_region_does_not_leak_credentials(self):
        """地区标签不能把带密码的地址带出去。"""
        from webui.app import _registration_proxy_name
        row = {"proxy_region": "1024·US",
               "proxy_used": "http://user:secret@1.2.3.4:9"}
        out = _registration_proxy_name(row, {})
        self.assertNotIn("secret", out)
        self.assertNotIn("@", out)

    def test_list_returns_region_field(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def _registration_proxy_name")
        seg = src[i:i + 1200]
        self.assertIn("proxy_region", seg)


if __name__ == "__main__":
    unittest.main()
