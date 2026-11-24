# -*- coding: utf-8 -*-
"""2026-11-24 18:00 自动订阅失败根因的回归测试。

覆盖 4 个环节：
  1. core/session.py: _GEO_CACHE 必须有 TTL，不能永不过期；失败探活不永久缓存 {}；
     提供 invalidate_geo_cache 供强制实时探活。
  2. core/auto_subscribe_scheduler.py: _verified_proxies_in_country 每次探活都绕过
     陈旧地理缓存，丢掉断气或飘国别的端口。
  3. core/team_checkout_service.py + auto_subscribe_scheduler.py:
     - generate_team_checkout_link 支持 proxy_candidates 显式多出口兜底切换；
     - start_subscription 先验出口、再选卡、再提链，长链成功后再 ensure_funds 充卡，
       避免出口断气时白给多张卡自动充值；且把提链跑通的出口置为支付首选。
  4. core/vcc_pay_session.py: 单席位结账页先跑 _login_cookies_for 拿到活出口，
     再用该活出口起浏览器（不再把浏览器死绑在初始坏出口上）。
"""
from __future__ import annotations

import types
import unittest
from unittest.mock import MagicMock, patch


class GeoCacheResilienceTest(unittest.TestCase):
    """_GEO_CACHE 不再永不过期，也不永久缓存空失败结果。"""

    def setUp(self):
        from core import session as sess_mod
        self.sess_mod = sess_mod
        sess_mod._GEO_CACHE.clear()
        if hasattr(sess_mod, "_GEO_CACHE_TS"):
            sess_mod._GEO_CACHE_TS.clear()

    def tearDown(self):
        self.sess_mod._GEO_CACHE.clear()
        if hasattr(self.sess_mod, "_GEO_CACHE_TS"):
            self.sess_mod._GEO_CACHE_TS.clear()

    def _make_stub_session(self, proxy: str, responses: list):
        """构造只测 _detect_exit_geo 的最小 BrowserSession 实例。"""
        bs = object.__new__(self.sess_mod.BrowserSession)
        bs.proxy = proxy
        it = iter(responses)

        def fake_get(url, headers=None, timeout=None):
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return item

        bs.session = types.SimpleNamespace(get=fake_get)
        return bs

    @staticmethod
    def _resp(data: dict, status: int = 200):
        return types.SimpleNamespace(status_code=status, json=lambda: data)

    def test_geo_cache_expires_after_ttl(self):
        """超过 TTL 后必须重新探测，不能把 2 小时前的 EG 当成现在的出口。"""
        proxy = "http://98.98.177.67:21048"
        r_eg = self._resp({"ip": "41.34.20.204", "country_code": "EG",
                           "city": "Giza", "timezone": "Africa/Cairo"})
        r_pe = self._resp({"ip": "190.238.57.29", "country_code": "PE",
                           "city": "Cusco", "timezone": "America/Lima"})
        clock = [1000.0]

        bs1 = self._make_stub_session(proxy, [r_eg])
        bs2 = self._make_stub_session(proxy, [r_pe])

        with patch("config.browser.AUTO_BROWSER_LOCALE_FROM_IP", True), \
             patch("config.browser.IP_GEO_ENDPOINTS", ["http://geo.test/1"]), \
             patch("config.browser.IP_GEO_TIMEOUT", 2), \
             patch.object(self.sess_mod.time, "monotonic", side_effect=lambda: clock[0]):
            g1 = bs1._detect_exit_geo()
            self.assertEqual(g1.get("country"), "EG")
            # 推进时钟超过 2 小时（7200s），必然过期
            clock[0] += 7200.0
            g2 = bs2._detect_exit_geo()
        self.assertEqual(g2.get("country"), "PE", "过期缓存应失效并重新探测真实国家")

    def test_failed_geo_probe_not_permanently_cached(self):
        """单次探测超时后，不应把空 dict 永久写进缓存锁死代理。"""
        proxy = "http://98.98.177.67:21049"
        r_eg = self._resp({"ip": "105.196.171.173", "country_code": "EG",
                           "city": "Cairo", "timezone": "Africa/Cairo"})
        bs_fail = self._make_stub_session(proxy, [TimeoutError("timeout")])
        bs_ok = self._make_stub_session(proxy, [r_eg])

        with patch("config.browser.AUTO_BROWSER_LOCALE_FROM_IP", True), \
             patch("config.browser.IP_GEO_ENDPOINTS", ["http://geo.test/1"]), \
             patch("config.browser.IP_GEO_TIMEOUT", 2):
            g1 = bs_fail._detect_exit_geo()
            self.assertEqual(g1, {})
            g2 = bs_ok._detect_exit_geo()
        self.assertEqual(g2.get("country"), "EG", "前一次失败不应毒化后续探测")

    def test_verified_proxies_in_country_bypasses_stale_cache(self):
        """即使 _GEO_CACHE 里有旧 EG 记录，_verified_proxies_in_country 也必须实时探活。"""
        from core import auto_subscribe_scheduler as sched

        p_dead = "http://98.98.177.67:21048"
        p_live = "http://98.98.177.67:21049"
        # 模拟 16:00 留下的陈旧缓存
        self.sess_mod._GEO_CACHE[p_dead] = {"ip": "41.34.20.204", "country": "EG"}
        self.sess_mod._GEO_CACHE[p_live] = {"ip": "105.196.171.173", "country": "EG"}

        probed = []

        class LiveProbeSession:
            def __init__(self, proxy=None, detect_exit_geo=True, fingerprint_seed=None):
                probed.append(proxy)
                # 模拟读取缓存：如果缓存没被清掉，就会直接返回旧 EG（复现事故）
                cached = self.sess_mod_ref._GEO_CACHE.get(proxy)
                if cached is not None:
                    self.exit_geo = dict(cached)
                elif proxy == p_dead:
                    self.exit_geo = {}  # 实际已超时断气
                else:
                    self.exit_geo = {"ip": "105.196.171.173", "country": "EG"}

            sess_mod_ref = self.sess_mod

            def close(self):
                pass

        with patch("core.session.BrowserSession", LiveProbeSession):
            got = sched._verified_proxies_in_country([p_dead, p_live], "EG")
        self.assertEqual(got, [p_live], "已断气的缓存出口不应通过实时验证")
        self.assertEqual(probed, [p_dead, p_live])


class CheckoutMultiProxyFallbackTest(unittest.TestCase):
    """generate_team_checkout_link 支持显式 proxy_candidates 依次兜底。"""

    def test_rotates_explicit_proxy_candidates_on_timeout(self):
        from core import team_checkout_service as svc

        tried = []

        def fake_native(token, cand, workspace, seats, country, currency,
                        promo, timeout, account_proxy):
            tried.append(cand)
            if cand == "http://98.98.177.67:21048":
                return None, None, "Timeout: Failed to perform, curl: (28) Connection timed out after 10000 milliseconds."
            return "https://chatgpt.com/checkout/openai_llc/oaics_ok", {"single_seat": True}, ""

        with patch.object(svc, "_native_checkout", side_effect=fake_native), \
             patch.object(svc, "fetch_1024proxy_ips") as fetch_mock, \
             patch.object(svc, "mark_promo_code"):
            res = svc.generate_team_checkout_link(
                token="tok",
                proxy_address="http://98.98.177.67:21048",
                proxy_candidates=["http://98.98.177.67:21048", "http://98.98.177.67:21049"],
                country="EG",
                currency="EGP",
                promo_assigned={"id": 1, "code": "PROMO123"},
                use_1024proxy=False,
                account_id=176,
                account_email="a@test",
                auto_pay=False,
            )
        fetch_mock.assert_not_called()
        self.assertTrue(res["ok"], f"应切到第二个活出口成功，实际：{res}")
        self.assertEqual(tried, ["http://98.98.177.67:21048", "http://98.98.177.67:21049"])
        self.assertEqual(res["proxy_used"], "http://98.98.177.67:21049")


class StartSubscriptionOrderAndProxyPromotionTest(unittest.TestCase):
    """start_subscription: 出口/提链失败绝不白充卡，提链胜出出口晋升为支付首选。"""

    def _base_cfg(self):
        return types.SimpleNamespace(
            VCC_1024PROXY_MINUTES=90,
            VCC_FUNDING_USD=18.0,
            VCC_MIN_CARD_BALANCE=0.0,
            VCC_USD_RATE=56.0,
            VCC_BALANCE_BUFFER=0.5,
            VCC_AUTO_RECHARGE=True,
            VCC_RECHARGE_AMOUNT=20.0,
            VCC_CARD_ID="",
            VCC_MAX_CHARGE=0.0,
            VCC_EXPECTED_CURRENCY="",
            VCC_AUTO_CONFIRM=False,
        )

    def _fake_card(self, tail="5290", bal=7.09):
        return {"cardId": f"cid-{tail}", "cardNoMask": f"537872****{tail}",
                "balance": bal, "cardStatus": "ACTIVE"}

    def test_no_live_proxy_does_not_recharge_or_pick_cards(self):
        """拿不到活出口时直接退码返回，既不充卡也不碰卡接口。"""
        from core import auto_subscribe_scheduler as sched

        ensure_mock = MagicMock()
        release_mock = MagicMock()
        client_mock = MagicMock()
        client_mock.list_cards.return_value = [self._fake_card()]

        with patch.object(sched, "_vcc_cfg", return_value=self._base_cfg()), \
             patch.object(sched, "_cfg", side_effect=lambda k, d=None: {
                 "AUTO_SUBSCRIBE_COUNTRY": "EG",
                 "AUTO_SUBSCRIBE_MAX_AMOUNT": 920.0,
             }.get(k, d)), \
             patch("core.db.get_account", return_value={"id": 176, "email": "a@t", "access_token": "tok"}), \
             patch("core.team_checkout_service.claim_next_promo_code", return_value={"id": 84, "code": "P1"}), \
             patch("core.team_checkout_service.release_promo_code", release_mock), \
             patch("core.team_checkout_service.fetch_1024proxy_ips", return_value=["98.98.177.67:21048"]), \
             patch.object(sched, "_verified_proxies_in_country", return_value=[]), \
             patch("core.vcc_client.build_client_from_config", return_value=client_mock), \
             patch("core.vcc_funds.ensure_funds", ensure_mock), \
             patch("core.vcc_funds.required_usd", return_value=18.0):
            res = sched.start_subscription(176)

        self.assertFalse(res["ok"])
        ensure_mock.assert_not_called()
        client_mock.list_cards.assert_not_called()
        release_mock.assert_called_once()

    def test_checkout_timeout_does_not_recharge_card(self):
        """提链失败时不应已经把卡充值了 20 USD。"""
        from core import auto_subscribe_scheduler as sched

        ensure_mock = MagicMock()
        release_mock = MagicMock()
        client_mock = MagicMock()
        client_mock.list_cards.return_value = [self._fake_card()]

        with patch.object(sched, "_vcc_cfg", return_value=self._base_cfg()), \
             patch.object(sched, "_cfg", side_effect=lambda k, d=None: {
                 "AUTO_SUBSCRIBE_COUNTRY": "EG",
                 "AUTO_SUBSCRIBE_MAX_AMOUNT": 920.0,
             }.get(k, d)), \
             patch("core.db.get_account", return_value={"id": 176, "email": "a@t", "access_token": "tok"}), \
             patch("core.team_checkout_service.claim_next_promo_code", return_value={"id": 84, "code": "P1"}), \
             patch("core.team_checkout_service.release_promo_code", release_mock), \
             patch("core.team_checkout_service.fetch_1024proxy_ips", return_value=["98.98.177.67:21048"]), \
             patch.object(sched, "_verified_proxies_in_country", return_value=["http://98.98.177.67:21048"]), \
             patch("core.vcc_client.build_client_from_config", return_value=client_mock), \
             patch.object(sched, "_addr_is_payable", return_value=True), \
             patch("core.vcc_funds.ensure_funds", ensure_mock), \
             patch("core.vcc_funds.required_usd", return_value=18.0), \
             patch("core.team_checkout_service.generate_team_checkout_link",
                   return_value={"ok": False, "error": "Timeout: curl: (28)"}):
            res = sched.start_subscription(176)

        self.assertFalse(res["ok"])
        ensure_mock.assert_not_called()
        release_mock.assert_called_once()

    def test_passes_all_proxies_to_checkout_and_promotes_winner(self):
        """提链传入全部活出口；若第 2 个出口提链成功，起支付会话时以它为首选。"""
        from core import auto_subscribe_scheduler as sched
        from core.vcc_pay_session import ST_DONE

        p1, p2, p3 = "http://1.1.1.1:1", "http://2.2.2.2:2", "http://3.3.3.3:3"
        client_mock = MagicMock()
        client_mock.list_cards.return_value = [self._fake_card(bal=30.0)]
        gen_calls = []
        sess_calls = []

        def fake_gen(**kw):
            gen_calls.append(kw)
            return {
                "ok": True,
                "url": "https://chatgpt.com/checkout/openai_llc/oaics_1",
                "seats": 1,
                "promo_used": "P1",
                "proxy_used": p2,
            }

        def fake_start_session(**kw):
            sess_calls.append(kw)
            return types.SimpleNamespace(sid="s1", state=ST_DONE, message="ok", gate_error="")

        with patch.object(sched, "_vcc_cfg", return_value=self._base_cfg()), \
             patch.object(sched, "_cfg", side_effect=lambda k, d=None: {
                 "AUTO_SUBSCRIBE_COUNTRY": "EG",
                 "AUTO_SUBSCRIBE_MAX_AMOUNT": 920.0,
             }.get(k, d)), \
             patch("core.db.get_account", return_value={"id": 176, "email": "a@t", "access_token": "tok"}), \
             patch("core.db.update_account_team_checkout"), \
             patch.object(sched, "note_card_used"), \
             patch("core.team_checkout_service.claim_next_promo_code", return_value={"id": 84, "code": "P1"}), \
             patch("core.team_checkout_service.fetch_1024proxy_ips", return_value=[p1, p2, p3]), \
             patch.object(sched, "_verified_proxies_in_country", return_value=[p1, p2, p3]), \
             patch("core.vcc_client.build_client_from_config", return_value=client_mock), \
             patch.object(sched, "_addr_is_payable", return_value=True), \
             patch("core.vcc_funds.ensure_funds", return_value=types.SimpleNamespace(recharged=0.0)), \
             patch("core.vcc_funds.required_usd", return_value=18.0), \
             patch("core.team_checkout_service.generate_team_checkout_link", side_effect=fake_gen), \
             patch("core.vcc_pay_session.start_session", side_effect=fake_start_session):
            res = sched.start_subscription(176)

        self.assertTrue(res["ok"])
        self.assertEqual(gen_calls[0].get("proxy_candidates"), [p1, p2, p3])
        self.assertEqual(sess_calls[0]["proxy"], p2)
        self.assertEqual(sess_calls[0]["extra_proxies"], [p1, p3])


class PaySessionBrowserProxySyncTest(unittest.TestCase):
    """单席位结账页：先登录拿活出口，再用该活出口启动浏览器。"""

    def test_login_cookies_for_reports_winning_proxy(self):
        from core import vcc_pay_session as ps

        p1, p2 = "http://98.98.177.67:21048", "http://98.98.177.67:21049"

        def preflight(email, proxy, fingerprint_state=None):
            if proxy == p1:
                raise RuntimeError("curl: (35) SSL_connect closed")
            s = MagicMock()
            s.session.cookies.get_dict.return_value = {
                "__Secure-next-auth.session-token": "tok-ok",
            }
            return s, "https://auth.openai.com/authorize"

        used = []
        with patch("core.db.get_account_by_email", return_value={"email_source": "outlook"}), \
             patch("core.account_liveness._network_preflight_with_retry", side_effect=preflight), \
             patch("core.openai_auth.follow_authorize"), \
             patch("core.account_liveness._login_via_password_or_otp"), \
             patch("core.session.close_browser_session"):
            cookies = ps._login_cookies_for("a@e.test", [p1, p2], gap=0, used_proxy_out=used)

        self.assertTrue(cookies)
        self.assertEqual(used, [p2])

    def test_worker_launches_browser_with_login_winning_proxy(self):
        """复现 18:00 #171：初始出口 p1 死掉，登录切到 p2 成功，浏览器必须用 p2 启动。"""
        from core import vcc_pay_session as ps

        p_dead = "http://98.98.177.67:21048"
        p_live = "http://98.98.177.67:21049"
        events = []

        def fake_login(email, proxies, *, rounds=6, gap=30.0, refetch=None, used_proxy_out=None):
            events.append(("login", list(proxies)))
            if used_proxy_out is not None:
                used_proxy_out.append(p_live)
            return [{"name": "__Secure-next-auth.session-token", "value": "v",
                     "domain": ".chatgpt.com", "path": "/", "secure": True}]

        class StopAfterBrowserLaunch(RuntimeError):
            pass

        def fake_cloak(cfg, proxy, sess):
            events.append(("cloak", proxy))
            raise StopAfterBrowserLaunch("stop test after verifying proxy")

        sess = ps.PaySession(sid="test171", account_id=171, email="a@e.test")
        sess._proxies = [p_dead, p_live]
        sess._refetch = None

        cfg = types.SimpleNamespace(
            VCC_SCREENSHOT_DIR="",
            VCC_CARD_ID="cid-0363",
            VCC_MIN_CARD_BALANCE=0.0,
            VCC_NAME_SOURCE="config",
            VCC_BILLING_NAME="Clay Foy",
            VCC_BILLING_COUNTRY="US",
            VCC_BILLING_STATE="MT",
            VCC_BILLING_CITY="Livingston",
            VCC_BILLING_LINE1="1201 Meadow",
            VCC_BILLING_LINE2="",
            VCC_BILLING_POSTAL="59047",
            VCC_FETCH_CARD_ADDRESS=False,
            VCC_USE_CLOAK=True,
            VCC_PAGE_TIMEOUT=30.0,
        )
        client = MagicMock()
        client.pick_card.return_value = {"cardId": "cid-0363", "cardNoMask": "474613****0363"}
        client.card_secrets.return_value = {"number": "4746130000000363", "exp_month": "01", "exp_year": "30", "cvc": "123"}

        with patch.object(ps, "_login_cookies_for", side_effect=fake_login), \
             patch.object(ps, "_try_cloak_browser", side_effect=fake_cloak):
            ps._worker(sess, "https://chatgpt.com/checkout/openai_llc/oaics_171",
                       p_dead, cfg, client)

        self.assertEqual([e[0] for e in events], ["login", "cloak"],
                         "单席位应先完成登录再启动浏览器")
        self.assertEqual(events[1][1], p_live,
                         "浏览器必须使用登录跑通的活出口，而不是死掉的初始出口")


if __name__ == "__main__":
    unittest.main()
