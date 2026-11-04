# -*- coding: utf-8 -*-
"""CloakBrowser 的 license 与内核必须落在挂载卷里。

事故：docker compose up -d 重建容器时，/root/.cloakbrowser 随 overlay 层一起
丢失，cloakbrowser login 的结果和已下载的内核全没了，注册直接报
CloakBrowserLicenseError。/root 不在任何挂载中。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = "/app/cache/cloakbrowser"


class CacheDirIsPersistedTest(unittest.TestCase):
    def setUp(self):
        self.compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_compose_redirects_cache_dir(self):
        self.assertIn(f"CLOAKBROWSER_CACHE_DIR={CACHE_DIR}", self.compose)

    def test_dockerfile_redirects_cache_dir(self):
        self.assertIn(f"CLOAKBROWSER_CACHE_DIR={CACHE_DIR}", self.dockerfile)

    def test_target_sits_under_a_mounted_volume(self):
        """重定向的目标必须真的在挂载里，否则等于没改。"""
        mounts = re.findall(r"^\s*-\s+\./([^:]+):(/[^\s:]+)",
                            self.compose, re.M)
        dests = [d for _, d in mounts]
        self.assertTrue(
            any(CACHE_DIR == d or CACHE_DIR.startswith(d.rstrip("/") + "/")
                for d in dests),
            f"{CACHE_DIR} 不在任何挂载下，挂载点：{dests}")

    def test_root_home_is_not_mounted(self):
        """记录事故成因：/root 没有挂载，所以默认路径必然丢。"""
        mounts = re.findall(r"^\s*-\s+\./[^:]+:(/[^\s:]+)", self.compose, re.M)
        self.assertFalse(any(d == "/root" or d.startswith("/root/")
                             for d in mounts))


class StartupWarnsOnMissingLicenseTest(unittest.TestCase):
    def setUp(self):
        self.entry = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    def test_checks_license_file(self):
        self.assertIn("license.key", self.entry)

    def test_honours_the_cache_dir_override(self):
        self.assertIn("CLOAKBROWSER_CACHE_DIR", self.entry)

    def test_check_runs_before_web_starts(self):
        self.assertLess(self.entry.index("license.key"),
                        self.entry.index("python web.py"))

    def test_missing_license_is_a_warning_not_a_hard_exit(self):
        """支付可以退回裸浏览器，不该因为缺 license 就拒绝启动整个服务。"""
        idx = self.entry.index("license.key")
        window = self.entry[idx:idx + 500]
        self.assertNotIn("exit 1", window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
