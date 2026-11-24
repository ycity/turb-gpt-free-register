# -*- coding: utf-8 -*-
"""支付出口必须和结算国家同区。

这是用真金白银换来的结论：结算货币和出口国家不在同一地区时，
Stripe 直接拒付。#74/#75 反复失败都是这个原因——卡是美国卡、
账号出口在台湾/美国，却按 EG 建单。

所以支付出口默认改成"按结算国家现取 1024proxy"，而不是
沿用账号的注册代理（它只保证能登录，不保证在结算国家）。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")


class ProxyTtlTest(unittest.TestCase):
    """1024proxy 默认只保留 10 分钟，支付一单常常超过。"""

    def test_fetch_accepts_minutes(self):
        from core.team_checkout_service import fetch_1024proxy_ips
        import inspect
        sig = inspect.signature(fetch_1024proxy_ips)
        self.assertIn("minutes", sig.parameters)

    def test_minutes_overrides_url_time(self):
        import inspect
        from core.team_checkout_service import fetch_1024proxy_ips
        src = inspect.getsource(fetch_1024proxy_ips)
        self.assertIn('query["time"]', src)

    def test_zero_keeps_url_default(self):
        """传 0 要保持 URL 里原有的 time，不能写成 0 分钟。"""
        import inspect
        from core.team_checkout_service import fetch_1024proxy_ips
        src = inspect.getsource(fetch_1024proxy_ips)
        i = src.index('query["time"]')
        self.assertIn("minutes", src[max(0, i - 160):i])
        self.assertIn("> 0", src[max(0, i - 160):i])

    def test_config_exists_and_long_enough(self):
        from config import vcc
        self.assertGreaterEqual(vcc.VCC_1024PROXY_MINUTES, 30)

    def test_config_has_env_override(self):
        src = (ROOT / "config" / "vcc.py").read_text(encoding="utf-8")
        self.assertIn("'VCC_1024PROXY_MINUTES': 'int'", src)

    def test_link_generation_uses_it(self):
        """提链拿到的代理接着就要用于支付，必须一起拉长。"""
        src = (ROOT / "core" / "team_checkout_service.py"
               ).read_text(encoding="utf-8")
        i = src.index("elif use_1024proxy:")
        self.assertIn("VCC_1024PROXY_MINUTES", src[i:i + 700])
        self.assertIn("minutes=", src[i:i + 700])


class PayExitFollowsCountryTest(unittest.TestCase):
    def test_backend_handles_special_value(self):
        self.assertIn("_PAY_1024_PREFIX", APP)
        i = APP.index("_PAY_1024_PREFIX =")
        self.assertIn("__1024__", APP[i:i + 120])

    def test_backend_uses_settlement_country(self):
        """取出口要按本单的结算国家，不是账单地址国家。"""
        i = APP.index("by_name.startswith(_PAY_1024_PREFIX)")
        blk = APP[i:i + 1400]
        self.assertIn('data.get("country")', blk)
        self.assertIn("fetch_1024proxy_ips", blk)

    def test_backend_verifies_real_exit(self):
        """1024proxy 按 region 取但偶尔给错国家，必须实测再用。"""
        self.assertIn("def _first_proxy_in_country", APP)
        i = APP.index("def _first_proxy_in_country")
        src = APP[i:i + 1800]
        self.assertIn("detect_exit_geo=True", src)
        self.assertIn("exit_geo", src)

    def test_backend_fails_loudly(self):
        """取不到对的国家要报错，不能默默退回注册代理——
        那样又会变成国家不一致被拒，而且看不出原因。"""
        i = APP.index("by_name.startswith(_PAY_1024_PREFIX)")
        blk = APP[i:i + 1400]
        self.assertIn("没给出可用的", blk)
        self.assertIn("400", blk)

    def test_backend_applies_ttl(self):
        i = APP.index("by_name.startswith(_PAY_1024_PREFIX)")
        blk = APP[i:i + 1400]
        self.assertIn("VCC_1024PROXY_MINUTES", blk)

    def test_frontend_option_exists(self):
        self.assertIn("PAY_1024_PREFIX", HTML)
        self.assertIn("_autoPay1024Option", HTML)

    def test_frontend_defaults_to_it(self):
        """默认必须选 1024proxy：注册代理不保证在结算国家。"""
        i = HTML.index("async function _autoPayLoadProxies")
        blk = HTML[i:HTML.index("function _autoPayProxyHintSync")]
        self.assertIn("sel.value = PAY_1024_PREFIX", blk)

    def test_frontend_survives_pool_failure(self):
        """代理池接口挂了也要留着 1024proxy 这条路。"""
        i = HTML.index("async function _autoPayLoadProxies")
        blk = HTML[i:HTML.index("function _autoPayProxyHintSync")]
        j = blk.index("catch (err)")
        self.assertIn("_autoPay1024Option", blk[j:j + 300])

    def test_hint_compares_with_settlement_country(self):
        """提示要比"出口 vs 结算国家"，不是"出口 vs 账单地址"——
        后者是卡的归属地，和能否付通是两回事。"""
        i = HTML.index("function _autoPayProxyHintSync")
        blk = HTML[i:HTML.index("function _autoPayRenderAddrOptions")]
        self.assertIn("_autoPayWantCc()", blk)
        self.assertIn("结算国家", blk)
        self.assertNotIn("_autoPayAddrCache", blk)

    def test_option_label_follows_country_input(self):
        """改结算国家要同步刷新选项文案，否则写着 EG 却按 US 取。"""
        self.assertIn("ccInput.oninput", HTML)
        i = HTML.index("ccInput.oninput")
        self.assertIn("_autoPayWantCc()", HTML[i:i + 400])

    def test_manual_pay_tip_mentions_settlement(self):
        i = HTML.index("const tip = $('#manualPayProxyTip')")
        blk = HTML[i:i + 600]
        self.assertIn("_autoPayWantCc()", blk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
