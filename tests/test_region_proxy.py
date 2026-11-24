# -*- coding: utf-8 -*-
"""查活按注册地区从代理池挑出口。

问题（2026-11-18 发现）：
  1024proxy 注册的号，出口是临时 IP，保留 120 分钟就失效。查活却仍用
  账号保存的 proxy_used，必然连不通；而 LIVE_CHECK_SWITCH_PROXY_ON_FAILURE
  默认关着（换出口会掩盖真实状态，见 config/proxy.py 注释），失败后不换
  出口直接判失败——于是拿到的是"网络失败"而不是账号真实死活。

修法：按账号注册时记的地区（proxy_region），从代理池挑同地区的长期出口；
池里没有该地区就回落 US。

实测代理池只覆盖 US/JP/SG/TW 四国，而注册用 9 个地区，所以 CA/DE/GB/AU/HK
这些会跨国回落——这是用户明确选定的取舍（保持注册地区分散）。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

from core import region_proxy as rp  # noqa: E402

_POOL = {
    "US": ["http://u:p@us1:1", "http://u:p@us2:1"],
    "JP": ["http://u:p@jp1:1"],
    "SG": ["http://u:p@sg1:1"],
    "TW": ["http://u:p@tw1:1"],
}


class ParseRegionTest(unittest.TestCase):
    def test_extracts_country(self):
        self.assertEqual(rp.parse_region("1024·CA"), "CA")
        self.assertEqual(rp.parse_region("1024·us"), "US")

    def test_bare_country_code(self):
        self.assertEqual(rp.parse_region("JP"), "JP")

    def test_rejects_pool_names(self):
        """代理池名称（Zoro-SG）不是地区，不能被当成 SG。"""
        self.assertEqual(rp.parse_region("Zoro-SG"), "")
        self.assertEqual(rp.parse_region("LazyCat-Starhub"), "")

    def test_blank(self):
        self.assertEqual(rp.parse_region(""), "")
        self.assertEqual(rp.parse_region(None), "")

    def test_rejects_non_two_letter(self):
        self.assertEqual(rp.parse_region("1024proxy"), "")
        self.assertEqual(rp.parse_region("1024·USA"), "")


class EphemeralTest(unittest.TestCase):
    def test_1024_region_is_ephemeral(self):
        self.assertTrue(rp.is_ephemeral_proxy("http://1.2.3.4:9", "1024·TW"))

    def test_bare_ip_is_ephemeral(self):
        """1024proxy 给的是裸 ip:port，不带认证。"""
        self.assertTrue(rp.is_ephemeral_proxy("http://1.2.3.4:9", ""))

    def test_pool_proxy_is_not(self):
        """代理池出口都带 user:pass@，是长期出口。"""
        self.assertFalse(rp.is_ephemeral_proxy("http://u:p@host:6611", ""))

    def test_blank_is_not(self):
        self.assertFalse(rp.is_ephemeral_proxy("", ""))


class PickExitTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(rp, "pool_exits_by_country", return_value=dict(_POOL))
        p.start()
        self.addCleanup(p.stop)

    def test_same_region_preferred(self):
        px, why = rp.pick_pool_exit("1024·JP")
        self.assertIn(px, _POOL["JP"])
        self.assertIn("同地区", why)

    def test_falls_back_to_us(self):
        """池里没有 CA，回落 US——这是用户选定的取舍。"""
        px, why = rp.pick_pool_exit("1024·CA")
        self.assertIn(px, _POOL["US"])
        self.assertIn("回落", why)

    def test_blank_region_also_falls_back(self):
        """proxy_region 上线前注册的号，出口同样失效，不能原样沿用。"""
        px, why = rp.pick_pool_exit("")
        self.assertIn(px, _POOL["US"])
        self.assertIn("无地区", why)

    def test_empty_pool_returns_blank(self):
        """池子没测过速时返回空，让调用方走原逻辑，别硬塞出口。"""
        with patch.object(rp, "pool_exits_by_country", return_value={}):
            px, why = rp.pick_pool_exit("1024·JP")
        self.assertEqual(px, "")
        self.assertIn("测速", why)

    def test_no_us_either(self):
        with patch.object(rp, "pool_exits_by_country",
                          return_value={"JP": ["http://u:p@jp1:1"]}):
            px, why = rp.pick_pool_exit("1024·CA")
        self.assertEqual(px, "")

    def test_spreads_across_same_region_exits(self):
        """同地区有多个时要分散，不能总用同一个。"""
        seen = {rp.pick_pool_exit("1024·US")[0] for _ in range(40)}
        self.assertGreater(len(seen), 1, "没有在同地区出口间分散")


class PoolGeoSourceTest(unittest.TestCase):
    def test_reads_from_test_results(self):
        """地区来自代理池测速结果，不靠名称猜。"""
        import inspect
        src = inspect.getsource(rp.pool_exits_by_country)
        self.assertIn("_load_results", src)
        self.assertIn("proxy_key", src)

    def test_ignores_untested_exits(self):
        """没测过的出口不计入，宁可少算也不猜。"""
        import inspect
        src = inspect.getsource(rp.pool_exits_by_country)
        self.assertIn('res.get("country")', src)

    def test_survives_missing_modules(self):
        """读不到代理池时返回空字典，不能抛异常把查活带崩。"""
        with patch.dict(sys.modules, {"webui.proxy_pool": None}):
            self.assertEqual(rp.pool_exits_by_country(), {})


class LiveCheckWiringTest(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "core" / "live_check_service.py").read_text(
            encoding="utf-8")

    def test_uses_region_proxy(self):
        # 换出口和固化合并进了 adopt_pool_exit，它内部调
        # is_ephemeral_proxy + pick_pool_exit
        self.assertIn("from core.region_proxy import", self.src)
        self.assertIn("adopt_pool_exit", self.src)

    def test_only_for_ephemeral(self):
        """长期出口（代理池注册的号）不该被换掉。

        判断在 adopt_pool_exit 内部，这里验证它确实先判临时再换。
        """
        import inspect
        from core import region_proxy
        src = inspect.getsource(region_proxy.adopt_pool_exit)
        self.assertIn("if not is_ephemeral_proxy", src)

    def test_only_when_no_explicit_proxy(self):
        """调用方显式指定出口时要尊重它，不能偷偷换。"""
        i = self.src.index("adopt_pool_exit")
        seg = self.src[max(0, i - 600):i]
        self.assertIn("proxy is None", seg)

    def test_failure_does_not_break_check(self):
        """选路失败要沿用原出口，不能让查活起不来。"""
        i = self.src.index("adopt_pool_exit")
        seg = self.src[i:i + 600]
        self.assertIn("except", seg)

    def test_logs_the_switch(self):
        """换了出口要写进查活日志，否则排查时看不出为什么换了 IP。"""
        self.assertIn("改用代理池", self.src)

    def test_marks_route_mode(self):
        """route 里要能看出是按地区选的，便于区分。"""
        self.assertIn("region_pool_exit", self.src)


if __name__ == "__main__":
    unittest.main()
