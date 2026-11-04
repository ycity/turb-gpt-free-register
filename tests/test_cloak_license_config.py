# -*- coding: utf-8 -*-
"""CloakBrowser license 配置正确性。

曾经的 bug：vcc_pay_session 从 `config.cloak` 读 license key，但真实模块叫
`config.cloakbrowser`。ImportError 被 except 吞掉，key 从来没传进去过，于是
支付每次都静默退回裸 Playwright——日志里只有一行 warning，很容易被忽略。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class LicenseModuleNameTest(unittest.TestCase):
    def test_config_module_is_cloakbrowser(self):
        """config.cloak 不存在，导入它必然 ImportError。"""
        with self.assertRaises(ImportError):
            __import__("config.cloak", fromlist=["x"])
        import config.cloakbrowser  # noqa: F401

    def test_pay_session_imports_the_real_module(self):
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        self.assertIn("from config import cloakbrowser as cloak_cfg", src)
        self.assertNotIn("from config import cloak as cloak_cfg", src)

    def test_driver_and_pay_read_the_same_key(self):
        """注册和支付必须读同一个配置项，否则一边能跑一边不能。"""
        pay = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        drv = (ROOT / "core"
               / "cloakbrowser_driver.py").read_text(encoding="utf-8")
        self.assertIn("CLOAK_LICENSE_KEY", pay)
        self.assertIn("CLOAK_LICENSE_KEY", drv)


class LicenseFailureIsLoudTest(unittest.TestCase):
    """读配置失败要留下日志；但 key 为空是正常配置，不该告警。"""

    def test_empty_key_is_not_an_error(self):
        """0.6.4 跑免费 binary 不需要 key，为空告警只是噪音。"""
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        start = src.index("def _try_cloak_browser")
        body = src[start:src.index("def _plain_browser")]
        self.assertNotIn("CLOAK_LICENSE_KEY 为空", body)

    def test_import_failure_is_logged(self):
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        start = src.index("def _try_cloak_browser")
        body = src[start:src.index("def _plain_browser")]
        idx = body.index("except Exception as exc")
        self.assertIn("logger.warning", body[idx:idx + 200])


class LicenseKeyIsConfigurableTest(unittest.TestCase):
    def test_env_override_is_registered(self):
        cfg = (ROOT / "config"
               / "cloakbrowser.py").read_text(encoding="utf-8")
        self.assertIn("'CLOAK_LICENSE_KEY': 'str'", cfg)

    def test_exposed_in_webui(self):
        editor = (ROOT / "webui"
                  / "config_editor.py").read_text(encoding="utf-8")
        self.assertIn("CLOAK_LICENSE_KEY", editor)


if __name__ == "__main__":
    unittest.main(verbosity=2)
