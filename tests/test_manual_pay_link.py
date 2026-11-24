# -*- coding: utf-8 -*-
"""手动支付面板必须读到刚生成的长链。

长链常常是本次支付会话刚生成并写进库的，而前端的 ACCOUNTS 缓存还是
打开页面时的旧数据。直接读缓存会错误显示"这个账号还没有长链"，
人就没法手动支付了。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


def _open_fn() -> str:
    i = HTML.index("async function _manualPayOpen")
    j = HTML.index("\nasync function _manualPayDone")
    return HTML[i:j]


class RouteTest(unittest.TestCase):
    def test_route_exists(self):
        self.assertIn('@app.get("/api/pay/checkout-link")', APP)

    def test_reads_from_db_not_cache(self):
        i = APP.index("def api_pay_checkout_link")
        body = APP[i:i + 1400]
        self.assertIn("db.get_account", body)

    def test_returns_url_and_time(self):
        i = APP.index("def api_pay_checkout_link")
        body = APP[i:i + 1400]
        self.assertIn("team_checkout_url", body)
        self.assertIn("team_checkout_created_at", body)

    def test_validates_account_id(self):
        i = APP.index("def api_pay_checkout_link")
        body = APP[i:i + 1400]
        self.assertIn("缺少 account_id", body)
        self.assertIn("账号不存在", body)


class FrontendTest(unittest.TestCase):
    def test_queries_backend(self):
        self.assertIn("/api/pay/checkout-link", _open_fn())

    def test_backend_takes_priority_over_cache(self):
        """缓存只能兜底，不能优先——优先读缓存就是这个 bug 本身。"""
        body = _open_fn()
        self.assertLess(body.index("/api/pay/checkout-link"),
                        body.index("acc.team_checkout_url"))

    def test_reuse_link_still_wins(self):
        """明确选了复用某条长链时，那条优先，不该被后端查询覆盖。"""
        body = _open_fn()
        self.assertLess(body.index("reuse.dataset.url"),
                        body.index("/api/pay/checkout-link"))

    def test_cache_remains_as_fallback(self):
        """后端请求失败时还能退回缓存，不要直接空着。"""
        body = _open_fn()
        self.assertIn("acc.team_checkout_url", body)
        self.assertIn("catch", body)

    def test_shows_loading_state(self):
        """查询要花时间，别让人盯着"还没有长链"误判。"""
        self.assertIn("正在读取长链", _open_fn())


if __name__ == "__main__":
    unittest.main(verbosity=2)
