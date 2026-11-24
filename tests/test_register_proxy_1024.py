# -*- coding: utf-8 -*-
"""注册出口改用 1024proxy：随机国家、保留 120 分钟、坏了换下一个 IP。

背景（2026-11-17）：
  固定代理池里那些长期出口被反复用于注册，IP 已被盯上——#85 之后注册的
  17 个号确认 9 个 account_deactivated、存活 0。

关键设计：
  1024proxy 给的 IP 不保证都能用（实测 11 个地区里 KR 那个 TLS 直接失败，
  其余 10 个正常）。坏的是**单个 IP**，所以失败后要换下一个 IP，
  而不是对同一个 IP 重连——重连只会把一次失败变成三次失败。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import register_proxy as rp  # noqa: E402


class _Cfg:
    REGISTER_PROXY_USE_1024 = True
    REGISTER_PROXY_REGIONS = ["US", "JP", "SG"]
    REGISTER_PROXY_MINUTES = 120
    REGISTER_PROXY_VERIFY = True
    REGISTER_PROXY_MAX_ATTEMPTS = 6
    REGISTER_PROXY_IPS_PER_REGION = 2


class AcquireTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(rp, "_cfg_value",
                         lambda n, d: getattr(_Cfg, n, d))
        p.start()
        self.addCleanup(p.stop)
        # 国家顺序本来是随机的（这是刻意的，见 RegionPoolTest）。
        # 但本类要断言"先试哪个、再试哪个"，必须固定顺序，否则测试时灵时不灵。
        p2 = patch.object(rp.random, "shuffle", lambda seq: None)
        p2.start()
        self.addCleanup(p2.stop)

    def _run(self, ips_by_region, verify_results, **kw):
        """verify_results: {proxy_url_substring: ok_bool}"""
        def fake_fetch(region, num, minutes=0):
            self.minutes_seen = minutes
            got = ips_by_region.get(region)
            if got is None:
                raise RuntimeError("该地区无货")
            return got[:num]

        def fake_verify(proxy, timeout=20.0):
            self.verified.append(proxy)
            for frag, ok in verify_results.items():
                if frag in proxy:
                    return {"ok": ok, "country": "XX",
                            "detail": "HTTP 200" if ok else "TLS 失败"}
            return {"ok": False, "country": "", "detail": "未配置"}

        self.verified = []
        self.minutes_seen = None
        mod = "core.team_checkout_service.fetch_1024proxy_ips"
        with patch(mod, side_effect=fake_fetch), \
             patch.object(rp, "verify_exit", side_effect=fake_verify):
            return rp.acquire_register_proxy(**kw)

    def test_returns_first_working_ip(self):
        res = self._run({"US": ["1.1.1.1:1", "2.2.2.2:2"],
                         "JP": ["3.3.3.3:3"], "SG": ["4.4.4.4:4"]},
                        {"1.1.1.1": True, "2.2.2.2": True,
                         "3.3.3.3": True, "4.4.4.4": True})
        self.assertTrue(res["ok"])
        self.assertTrue(res["proxy"].startswith("http://"))
        self.assertEqual(len(self.verified), 1, "第一个就能用时不该多试")

    def test_rotates_to_next_ip_when_one_fails(self):
        """这是本次的核心：坏了换下一个 IP，不是重连同一个。"""
        res = self._run({"US": ["1.1.1.1:1", "2.2.2.2:2"],
                         "JP": ["3.3.3.3:3"], "SG": ["4.4.4.4:4"]},
                        {"1.1.1.1": False, "2.2.2.2": True,
                         "3.3.3.3": True, "4.4.4.4": True})
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.verified), 2)
        # 两次验证必须是不同的 IP，不能是同一个重试
        self.assertNotEqual(self.verified[0], self.verified[1],
                            "失败后应换 IP，而不是重连同一个")

    def test_crosses_region_when_whole_region_bad(self):
        """一个国家的 IP 全坏时要换国家继续，别卡死在一个地区。"""
        res = self._run({"US": ["1.1.1.1:1", "1.1.1.2:2"],
                         "JP": ["3.3.3.3:3"], "SG": ["4.4.4.4:4"]},
                        {"1.1.1.": False, "3.3.3.3": True, "4.4.4.4": True})
        self.assertTrue(res["ok"])
        self.assertIn("3.3.3.3", res["proxy"] + str(res["tried"]))

    def test_region_without_stock_is_skipped(self):
        """某地区取不到 IP（接口报错）时跳过，不能整体失败。"""
        res = self._run({"JP": ["3.3.3.3:3"]},   # US/SG 无货
                        {"3.3.3.3": True})
        self.assertTrue(res["ok"])

    def test_all_bad_reports_failure(self):
        res = self._run({"US": ["1.1.1.1:1"], "JP": ["3.3.3.3:3"],
                         "SG": ["4.4.4.4:4"]},
                        {"1.1.1.1": False, "3.3.3.3": False, "4.4.4.4": False})
        self.assertFalse(res["ok"])
        self.assertEqual(res["proxy"], "")
        self.assertTrue(res["tried"], "失败时要报告试过哪些，便于排查")

    def test_respects_attempt_cap(self):
        """不能无限试下去，否则注册迟迟发不起来。"""
        res = self._run({"US": ["1.1.1.1:1", "1.1.1.2:2"],
                         "JP": ["3.3.3.1:1", "3.3.3.2:2"],
                         "SG": ["4.4.4.1:1", "4.4.4.2:2"]},
                        {"never": True}, attempts=3)
        self.assertFalse(res["ok"])
        self.assertLessEqual(len(self.verified), 3)

    def test_requests_120_minutes(self):
        """保留时长要顶满，2FA 和查席位都得在这段时间内跑完。"""
        self._run({"US": ["1.1.1.1:1"]}, {"1.1.1.1": True})
        self.assertEqual(self.minutes_seen, 120)

    def test_skip_verify_returns_immediately(self):
        res = self._run({"US": ["1.1.1.1:1"]}, {}, verify=False)
        self.assertTrue(res["ok"])
        self.assertEqual(self.verified, [], "关掉验证就不该再去连")

    def test_normalizes_bare_ip_port(self):
        """接口返回裸 ip:port，必须补 scheme。

        不补的话 curl 会当成 https 去握手，报 WRONG_VERSION_NUMBER——
        今天测 EGP 时就踩过这个。
        """
        res = self._run({"US": ["1.1.1.1:8080"]}, {"1.1.1.1": True})
        self.assertTrue(res["proxy"].startswith("http://"))


class RegionPoolTest(unittest.TestCase):
    def test_regions_are_shuffled(self):
        """国家要随机，否则所有号都挤在同一个地区。"""
        with patch.object(rp, "_cfg_value",
                          lambda n, d: (["A", "B", "C", "D", "E", "F", "G", "H"]
                                        if n == "REGISTER_PROXY_REGIONS" else d)):
            seen = {tuple(rp._candidate_regions()) for _ in range(30)}
        self.assertGreater(len(seen), 1, "顺序一成不变就不是随机")

    def test_falls_back_to_builtin_list(self):
        with patch.object(rp, "_cfg_value", lambda n, d: [] if n == "REGISTER_PROXY_REGIONS" else d):
            self.assertTrue(rp._candidate_regions())

    def test_known_bad_region_not_in_default(self):
        """KR 实测 TLS 失败，不该进默认列表。"""
        from config import register_proxy as cfg
        self.assertNotIn("KR", cfg.REGISTER_PROXY_REGIONS)


class VerifyExitTest(unittest.TestCase):
    def test_403_counts_as_usable(self):
        """403 是业务层拦截，网络是通的，注册流程自己会处理挑战页。"""
        import inspect
        src = inspect.getsource(rp.verify_exit)
        self.assertIn("403", src, "要说明 403 为什么算可用")

    def test_checks_chatgpt_not_ip_service(self):
        """能连 ip-api 不代表能连 OpenAI，必须验目的地本身。"""
        self.assertIn("chatgpt.com", rp._VERIFY_URL)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        from config import register_proxy as cfg
        self.assertTrue(cfg.REGISTER_PROXY_USE_1024)
        self.assertEqual(cfg.REGISTER_PROXY_MINUTES, 120)
        self.assertTrue(cfg.REGISTER_PROXY_VERIFY)

    def test_minutes_within_vendor_limit(self):
        """1024proxy 上限 120，超了接口会拒。"""
        from config import register_proxy as cfg
        self.assertLessEqual(cfg.REGISTER_PROXY_MINUTES, 120)
        self.assertGreater(cfg.REGISTER_PROXY_MINUTES, 0)

    def test_has_kill_switch(self):
        """出问题要能一键回滚到旧代理池。"""
        from config import register_proxy as cfg
        self.assertIn("REGISTER_PROXY_USE_1024", dir(cfg))


class DriverWiringTest(unittest.TestCase):
    def test_driver_uses_new_acquirer(self):
        import inspect
        from core import cloakbrowser_driver as drv
        src = inspect.getsource(drv.build_cloak_driver)
        self.assertIn("_acquire_registration_exit()", src)

    def test_falls_back_to_pool_on_failure(self):
        """1024proxy 抽风时要回落代理池，不能让注册完全停摆。"""
        import inspect
        from core import cloakbrowser_driver as drv
        src = inspect.getsource(drv._acquire_registration_exit)
        self.assertIn("pick_proxy", src)
        self.assertIn("REGISTER_PROXY_USE_1024", src)

    def test_fallback_on_exception(self):
        """1024proxy 抛异常时必须回落，不能把注册带崩。"""
        from config import register_proxy as cfg
        from core import cloakbrowser_driver as drv

        with patch.object(cfg, "REGISTER_PROXY_USE_1024", True), \
             patch("core.register_proxy.acquire_register_proxy",
                   side_effect=RuntimeError("1024 挂了")), \
             patch("config.proxy.pick_proxy", return_value="http://pool:1"):
            self.assertEqual(drv._acquire_registration_exit()[0], "http://pool:1")

    def test_fallback_when_no_usable_exit(self):
        """取到了但全都不可用时，也要回落而不是返回空出口。"""
        from config import register_proxy as cfg
        from core import cloakbrowser_driver as drv

        with patch.object(cfg, "REGISTER_PROXY_USE_1024", True), \
             patch("core.register_proxy.acquire_register_proxy",
                   return_value={"ok": False, "proxy": "", "detail": "全坏"}), \
             patch("config.proxy.pick_proxy", return_value="http://pool:3"):
            self.assertEqual(drv._acquire_registration_exit()[0], "http://pool:3")

    def test_fallback_when_disabled(self):
        """开关关掉时压根不该去碰 1024proxy。

        注意不能用 patch.dict(sys.modules, ...) 来伪造 config.register_proxy：
        `from config import register_proxy` 走的是 config 包上已经设好的
        属性，不读 sys.modules，伪造不生效——之前这条就是这样"通过"的，
        实际跑的是真配置（开关为 True），等于什么都没测。
        """
        from config import register_proxy as cfg
        from core import cloakbrowser_driver as drv

        with patch.object(cfg, "REGISTER_PROXY_USE_1024", False), \
             patch("core.register_proxy.acquire_register_proxy",
                   side_effect=AssertionError("开关关了还去取 1024proxy")), \
             patch("config.proxy.pick_proxy", return_value="http://pool:2"):
            self.assertEqual(drv._acquire_registration_exit()[0], "http://pool:2")

    def test_uses_1024_when_enabled(self):
        """开关开着且取到可用出口时，就该用它，不能还去摸代理池。"""
        from config import register_proxy as cfg
        from core import cloakbrowser_driver as drv

        with patch.object(cfg, "REGISTER_PROXY_USE_1024", True), \
             patch("core.register_proxy.acquire_register_proxy",
                   return_value={"ok": True, "proxy": "http://1024:9",
                                 "country": "US"}), \
             patch("config.proxy.pick_proxy",
                   side_effect=AssertionError("不该回落代理池")):
            self.assertEqual(drv._acquire_registration_exit()[0], "http://1024:9")


if __name__ == "__main__":
    unittest.main()
