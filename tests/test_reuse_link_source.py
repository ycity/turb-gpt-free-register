# -*- coding: utf-8 -*-
"""复用长链时必须以后端为准，不能信前端缓存。

踩过的坑：#75 的长链从 EG/EGP 换成 US/USD 重新生成后，
在界面上点「复用」打开的还是旧的 EGP 页面——因为 ACCOUNTS
缓存是打开页面时加载的，期间长链已经变了。

probe-link 本来就返回后端当前记录的 url，用它即可。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")


def _reuse_block():
    """自动支付里判断能否复用长链那一段。"""
    i = HTML.index("const savedUrl = String(acc.team_checkout_url")
    return HTML[i:i + 2200]


class ReuseUsesBackendTest(unittest.TestCase):
    def test_backend_url_preferred(self):
        """复用的 url 必须来自 probe 返回，不能直接用缓存。"""
        blk = _reuse_block()
        self.assertIn("probe.url", blk)
        i = blk.index("_autoPayUseExistingLink(")
        call = blk[i:i + 120]
        self.assertIn("useUrl", call)
        self.assertNotIn("savedUrl,", call)

    def test_cache_only_as_fallback(self):
        """后端没返回 url 时才退回缓存，顺序不能反。"""
        blk = _reuse_block()
        i = blk.index("const useUrl")
        line = blk[i:i + 120]
        self.assertLess(line.index("probe.url"), line.index("savedUrl"))

    def test_created_at_also_from_backend(self):
        """时间戳也要跟着更新，否则提示里显示的是旧长链的时间。"""
        blk = _reuse_block()
        self.assertIn("probe.created_at", blk)

    def test_shows_what_will_be_paid(self):
        """确认框里要写清这条链的内容（几席、多少钱），
        否则换过地区/货币后用户看不出差别。"""
        blk = _reuse_block()
        self.assertIn("probe.reason", blk)

    def test_probe_returns_url(self):
        """后端得真的返回 url，前端才有得用。"""
        i = APP.index("def api_pay_probe_link")
        body = APP[i:i + 1600]
        self.assertIn('"url": url', body)
        self.assertIn('"created_at"', body)

    def test_manual_pay_path_also_asks_backend(self):
        """手动支付那条路径同样不能只信缓存。"""
        i = HTML.index("const reuse = $('#apReuseLink')")
        blk = HTML[i:i + 900]
        self.assertIn("/api/pay/checkout-link", blk)
        # 缓存只在后端读不到时兜底
        self.assertLess(blk.index("/api/pay/checkout-link"),
                        blk.index("acc.team_checkout_url"))

    def test_only_two_cache_reads(self):
        """多一处直接读缓存就多一处踩同样的坑。"""
        self.assertEqual(HTML.count("acc.team_checkout_url"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
