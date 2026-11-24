# -*- coding: utf-8 -*-
"""席位探测要和查活一样处理失效的临时出口。

问题（2026-11-18 发现）：
  1024proxy 注册的号，proxy_used 是临时 IP，保留 120 分钟就失效。
  查活已经会识别并换成代理池同地区出口，但席位探测漏了这一层——
  它直接用 proxy_used，连不通后探不出出口国家，报成
  "出口国家 未知 没有内置货币映射"，看着像账号问题。

  实际受害：#109 #121 都是这个报错；#122 注册 1.5 小时后出口已失效。

修法：probe_single_seat 复用 region_proxy 的同一套逻辑。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class ExitSelectionTest(unittest.TestCase):
    """选出口的行为。"""

    def _probe(self, account, pool_exit="http://u:p@pool:6611"):
        """跑到创建 BrowserSession 为止，返回实际传入的 proxy。

        probe_single_seat 内部是 `from core.session import BrowserSession`，
        所以必须 patch core.session 模块上的属性本身——patch 到别处不生效，
        会让测试真的打网络（第一版就是这样，跑了 18 秒）。
        """
        from core import session as session_mod
        from core import single_seat
        captured = {}

        class FakeSession:
            def __init__(self, proxy=None, **kw):
                captured["proxy"] = proxy
                # 让后续流程立刻停下，不发任何请求
                raise RuntimeError("stop-here")

        with patch.object(session_mod, "BrowserSession", FakeSession), \
             patch("core.region_proxy.pick_pool_exit",
                   return_value=(pool_exit, "同地区 US")):
            try:
                single_seat.probe_single_seat(account)
            except RuntimeError as exc:
                if "stop-here" not in str(exc):
                    raise
        return captured.get("proxy")

    def test_swaps_ephemeral_exit(self):
        """临时出口要被换掉。"""
        got = self._probe({
            "access_token": "t",
            "proxy_used": "http://128.1.12.151:24340",
            "proxy_region": "1024·US",
        })
        self.assertIn("pool", str(got), "临时出口没被换成代理池出口")

    def test_keeps_pool_exit(self):
        """代理池的长期出口不该被换。"""
        got = self._probe({
            "access_token": "t",
            "proxy_used": "http://yun:pw@61.13.236.169:6611",
            "proxy_region": "",
        })
        self.assertIn("61.13.236.169", str(got), "长期出口被误换了")

    def test_blank_proxy_stays_blank(self):
        got = self._probe({"access_token": "t", "proxy_used": ""})
        self.assertIn(got, (None, ""), "没有代理时不该硬塞一个")


class WiringTest(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "core" / "single_seat.py").read_text(
            encoding="utf-8")

    def test_uses_region_proxy(self):
        # 换出口和固化合并进了 adopt_pool_exit
        self.assertIn("from core.region_proxy import", self.src)
        self.assertIn("adopt_pool_exit", self.src)

    def test_only_swaps_ephemeral(self):
        """长期出口不该被换掉；判断在 adopt_pool_exit 内部。"""
        import inspect
        from core import region_proxy
        src = inspect.getsource(region_proxy.adopt_pool_exit)
        self.assertIn("if not is_ephemeral_proxy", src)

    def test_failure_falls_back(self):
        """选路失败要沿用原出口，不能让探测起不来。"""
        i = self.src.index("adopt_pool_exit")
        seg = self.src[i:i + 600]
        self.assertIn("except", seg)

    def test_logs_the_swap(self):
        """换了出口要记日志，否则排查时看不出为什么换了 IP。"""
        self.assertIn("改用代理池", self.src)

    def test_region_read_from_account(self):
        self.assertIn('account.get("proxy_region")', self.src)


class ConsistencyTest(unittest.TestCase):
    """两条路径要用同一套逻辑。"""

    def test_same_helper_as_live_check(self):
        live = (ROOT / "core" / "live_check_service.py").read_text(
            encoding="utf-8")
        seat = (ROOT / "core" / "single_seat.py").read_text(encoding="utf-8")
        twofa = (ROOT / "core" / "twofa_service.py").read_text(
            encoding="utf-8")
        # 三条路径必须走同一个入口，否则又会出现"修了两条漏一条"
        for name, src in (("查活", live), ("席位", seat), ("2FA", twofa)):
            self.assertIn("adopt_pool_exit", src,
                          "%s 路径没接上固化逻辑" % name)


if __name__ == "__main__":
    unittest.main()
