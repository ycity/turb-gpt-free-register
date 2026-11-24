# -*- coding: utf-8 -*-
"""临时出口换成代理池出口后，要固化为账号的注册代理。

需求（2026-11-18）：1024proxy 注册的号，只要查活/2FA/席位探测时改用了
邮箱池（代理池）的出口，就把注册代理改成那个池出口。

原因：1024proxy 的出口保留 120 分钟就失效，而这些操作往往发生在几小时
甚至几天后。固化之后，后续操作都走同一个稳定出口，不用每次重新挑，
账号的网络指纹也保持一致——反复换出口本身就像盗号。

三条路径必须一致（历史上是逐个补的，漏过两次）：
  查活    live_check_service   2026-11-18 已接
  席位    single_seat          2026-11-18 已接
  2FA     twofa_service        本次补上（#114 就栽在这，连续 3 次 403）
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

from core import region_proxy as rp  # noqa: E402

_POOL = "http://u:p@pool-sg:6611"


class AdoptTest(unittest.TestCase):
    def _run(self, rows, aid, proxy, region, picked=_POOL):
        from core import db
        with patch.object(rp, "pick_pool_exit",
                          return_value=(picked, "同地区 SG")), \
             patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts") as save:
            got, why = rp.adopt_pool_exit(aid, proxy, region)
        return got, why, save

    def test_swaps_and_persists(self):
        """临时出口要换掉，并写回 proxy_used。"""
        rows = [{"id": 7, "proxy_used": "http://1.2.3.4:9",
                 "proxy_region": "1024·SG"}]
        got, _, save = self._run(rows, 7, "http://1.2.3.4:9", "1024·SG")
        self.assertEqual(got, _POOL)
        self.assertEqual(rows[0]["proxy_used"], _POOL, "没写回账号")
        save.assert_called_once()

    def test_clears_1024_region_tag(self):
        """地区标记要换掉，否则下次又被当成临时出口，白换一轮。"""
        rows = [{"id": 7, "proxy_used": "http://1.2.3.4:9",
                 "proxy_region": "1024·SG"}]
        self._run(rows, 7, "http://1.2.3.4:9", "1024·SG")
        tag = rows[0]["proxy_region"]
        self.assertFalse(tag.startswith("1024"), "还是 1024 前缀：%s" % tag)
        self.assertFalse(rp.is_ephemeral_proxy(rows[0]["proxy_used"], tag),
                         "换完还被判成临时出口")

    def test_keeps_country_info(self):
        """国家决定定价区，固化后不能丢。"""
        rows = [{"id": 7, "proxy_used": "http://1.2.3.4:9",
                 "proxy_region": "1024·SG"}]
        self._run(rows, 7, "http://1.2.3.4:9", "1024·SG")
        self.assertIn("SG", rows[0]["proxy_region"])

    def test_pool_proxy_untouched(self):
        """已经是代理池出口的，不该动。"""
        rows = [{"id": 7, "proxy_used": "http://u:p@old:6611"}]
        got, _, save = self._run(rows, 7, "http://u:p@old:6611", "")
        self.assertEqual(got, "http://u:p@old:6611")
        save.assert_not_called()

    def test_no_pool_exit_keeps_original(self):
        """池里挑不出出口时，原样返回，不能把账号写成空代理。"""
        rows = [{"id": 7, "proxy_used": "http://1.2.3.4:9"}]
        got, why, save = self._run(rows, 7, "http://1.2.3.4:9",
                                   "1024·XX", picked="")
        self.assertEqual(got, "http://1.2.3.4:9")
        save.assert_not_called()

    def test_write_failure_still_returns_exit(self):
        """写库失败不影响本次操作——拿到能用的出口才是主要目的。"""
        from core import db
        with patch.object(rp, "pick_pool_exit",
                          return_value=(_POOL, "同地区 SG")), \
             patch.object(db, "_load_accounts",
                          side_effect=RuntimeError("db 挂了")):
            got, _ = rp.adopt_pool_exit(7, "http://1.2.3.4:9", "1024·SG")
        self.assertEqual(got, _POOL, "写库失败就不给出口了？")


class WiringTest(unittest.TestCase):
    """三条路径都要接上，一条都不能漏。"""

    def test_live_check_adopts(self):
        src = (ROOT / "core" / "live_check_service.py").read_text(
            encoding="utf-8")
        self.assertIn("adopt_pool_exit", src)

    def test_single_seat_adopts(self):
        src = (ROOT / "core" / "single_seat.py").read_text(encoding="utf-8")
        self.assertIn("adopt_pool_exit", src)

    def test_twofa_adopts(self):
        """2FA 是最后补上的那条——#114 连续 3 次 403 就栽在这。"""
        src = (ROOT / "core" / "twofa_service.py").read_text(encoding="utf-8")
        self.assertIn("adopt_pool_exit", src)

    def test_twofa_swaps_before_session(self):
        """必须在建会话之前换，否则换了也没用。"""
        src = (ROOT / "core" / "twofa_service.py").read_text(encoding="utf-8")
        a = src.index("adopt_pool_exit")
        b = src.index("real_proxy, relay, proxy_source = _resolve_twofa_proxy")
        self.assertLess(a, b, "换出口要在解析传输代理之前")

    def test_all_paths_pass_label(self):
        """带上来源标签，日志里能看出是哪条路径触发的固化。"""
        for mod in ("live_check_service", "single_seat", "twofa_service"):
            src = (ROOT / "core" / ("%s.py" % mod)).read_text(encoding="utf-8")
            i = src.index("adopt_pool_exit(")
            seg = src[i:i + 320]
            self.assertIn("label=", seg, "%s 没传 label" % mod)

    def test_failures_are_swallowed(self):
        """选路失败不能让主流程起不来。"""
        for mod in ("live_check_service", "single_seat", "twofa_service"):
            src = (ROOT / "core" / ("%s.py" % mod)).read_text(encoding="utf-8")
            i = src.index("adopt_pool_exit")
            seg = src[i:i + 700]
            self.assertIn("except", seg, "%s 没有兜底" % mod)


if __name__ == "__main__":
    unittest.main()
