# -*- coding: utf-8 -*-
"""会话跑起来之后，仍要看得见本单用的是哪个出口。

发现过程：出口选择器放在配置区（#autoPaySetup），而会话一开始填表这块就
整个隐藏，于是人在"需要人机验证"那一屏完全找不到它，也看不出当前走的是
注册代理还是临时指定的出口。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


class UsedProxyIsVisibleTest(unittest.TestCase):
    def test_tracks_the_chosen_proxy(self):
        self.assertIn("_autoPayUsedProxyName", HTML)

    def test_info_bar_shows_it(self):
        start = HTML.index("function _autoPayRenderWho")
        body = HTML[start:HTML.index("function _autoPayOpenModal")]
        self.assertIn("_autoPayUsedProxyName", body)
        self.assertIn("本单指定", body)

    def test_info_bar_still_names_the_registration_proxy(self):
        """换了出口也要能看出原本的注册代理是哪个。"""
        start = HTML.index("function _autoPayRenderWho")
        body = HTML[start:HTML.index("function _autoPayOpenModal")]
        self.assertIn("register_proxy_name", body)
        self.assertIn("注册代理为", body)

    def test_frozen_at_submit_time(self):
        """提交后下拉会被隐藏，取值必须在那之前固化。"""
        start = HTML.index("async function _autoPayStart")
        body = HTML[start:start + 1600]
        freeze = body.index("_autoPayUsedProxyName = $('#autoPayProxySelect')")
        payload = body.index("pay_proxy_name") if "pay_proxy_name" in body else len(body)
        self.assertLess(freeze, payload)

    def test_payload_uses_the_frozen_value(self):
        self.assertIn("pay_proxy_name: _autoPayUsedProxyName", HTML)

    def test_reset_when_opening_a_fresh_modal(self):
        """不重置的话，上一单的出口会串到下一单的信息栏。"""
        start = HTML.index("function _autoPayOpenModal")
        body = HTML[start:start + 500]
        self.assertIn("_autoPayUsedProxyName = ''", body)

    def test_reset_only_in_setup_mode(self):
        """回看历史会话时不该把出口清掉。"""
        start = HTML.index("function _autoPayOpenModal")
        body = HTML[start:start + 500]
        idx = body.index("_autoPayUsedProxyName = ''")
        self.assertIn("if (setup)", body[max(0, idx - 60):idx])


class SelectorIsDiscoverableTest(unittest.TestCase):
    def test_explains_it_hides_after_start(self):
        idx = HTML.index('id="autoPayProxySelect"')
        window = HTML[idx:idx + 500]
        self.assertIn("开始填表后这块会隐藏", window)

    def test_tells_how_to_get_it_back(self):
        idx = HTML.index('id="autoPayProxySelect"')
        window = HTML[idx:idx + 500]
        self.assertIn("取消并关闭", window)

    def test_reassures_promo_is_not_wasted(self):
        """怕重复消耗优惠码就不敢取消重开，要说清楚。"""
        idx = HTML.index('id="autoPayProxySelect"')
        window = HTML[idx:idx + 500]
        self.assertIn("长链可复用", window)


class ManualPanelProxyTipTest(unittest.TestCase):
    def test_tip_element_exists(self):
        self.assertIn('id="manualPayProxyTip"', HTML)

    def test_says_server_proxy_does_not_apply(self):
        start = HTML.index("async function _manualPayOpen")
        body = HTML[start:HTML.index("async function _manualPayDone")]
        self.assertIn("服务端出口", body)
        self.assertIn("不生效", body)

    def test_names_the_billing_country(self):
        start = HTML.index("async function _manualPayOpen")
        body = HTML[start:HTML.index("async function _manualPayDone")]
        self.assertIn("账单地址在", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
