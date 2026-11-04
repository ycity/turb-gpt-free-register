# -*- coding: utf-8 -*-
"""cloakbrowser 版本必须钉死。

事故：Dockerfile 写的是 cloakbrowser>=0.4.10，容器重建时 pip 解析成 0.7.0。
0.7.0 起免费 binary 也要先领 license key，于是注册报 CloakBrowserLicenseError、
支付静默退回裸 Playwright。0.6.4 直接跑免费 binary(v146)，无需任何 key。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
PINNED = "0.6.4"


def _specs(text: str) -> list[str]:
    """抓出所有 cloakbrowser 的版本约束。"""
    return re.findall(r'cloakbrowser(?:\[[^\]]*\])?\s*([<>=!~][^"\s\\]*)', text)


class VersionIsPinnedTest(unittest.TestCase):
    def test_dockerfile_pins_exact_version(self):
        specs = _specs((ROOT / "Dockerfile").read_text(encoding="utf-8"))
        self.assertTrue(specs, "Dockerfile 里找不到 cloakbrowser 约束")
        for s in specs:
            self.assertEqual(s, f"=={PINNED}", f"Dockerfile 约束不是钉死：{s}")

    def test_requirements_pins_exact_version(self):
        specs = _specs((ROOT / "requirements.txt").read_text(encoding="utf-8"))
        self.assertTrue(specs, "requirements.txt 里找不到 cloakbrowser 约束")
        for s in specs:
            self.assertEqual(s, f"=={PINNED}", f"requirements 约束不是钉死：{s}")

    def test_both_files_agree(self):
        d = _specs((ROOT / "Dockerfile").read_text(encoding="utf-8"))
        r = _specs((ROOT / "requirements.txt").read_text(encoding="utf-8"))
        self.assertEqual(set(d), set(r), "两处版本约束不一致")

    def test_no_open_ended_constraint_remains(self):
        """>= 会在重建时悄悄升级，这正是事故成因。"""
        for name in ("Dockerfile", "requirements.txt"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for s in _specs(text):
                self.assertNotIn(">=", s, f"{name} 仍有开放约束：{s}")


class PinIsExplainedTest(unittest.TestCase):
    """钉死版本没有注释就会被下一个人顺手升回去。"""

    def test_dockerfile_explains_why(self):
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        idx = text.index(f"cloakbrowser[geoip]=={PINNED}")
        window = text[max(0, idx - 500):idx]
        self.assertIn("license", window.lower())

    def test_requirements_explains_why(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        idx = text.index(f"cloakbrowser[geoip]=={PINNED}")
        window = text[max(0, idx - 200):idx]
        self.assertIn("license", window.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
