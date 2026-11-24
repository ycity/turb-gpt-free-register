# -*- coding: utf-8 -*-
"""ant 注册适配层 + 自动化模块 + 节点登录流程的测试。全 mock：不碰数据库、网络、HK 节点。

运行: python tests/test_ant_registration.py
（容器里测试文件经 docker cp 放在 /tmp 时也能跑，项目根目录按 /app 找）
"""
import argparse
import contextlib
import os
import sys
import unittest
from unittest import mock

# 容器里项目在 /app；在仓库里则用仓库根目录（本文件的上一级）
for _root in ("/app", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if os.path.isdir(os.path.join(_root, "core")) and _root not in sys.path:
        sys.path.insert(0, _root)

import config.proxy as proxy_cfg  # noqa: E402
import core.account_export as account_export  # noqa: E402
import core.ant_node_bridge as nb  # noqa: E402
import core.ant_registration_bridge as bridge  # noqa: E402
import core.ant_auto_register as auto_reg  # noqa: E402
import core.email_provider as email_provider  # noqa: E402


def _is_otp_css(css: str) -> bool:
    return ("one-time-code" in css) or ("name='code'" in css) or ("numeric" in css)


def _flow_info(**extra):
    info = {
        "access_token": "tok", "user_id": "user-x", "user_name": "Ann",
        "expires": "2027-01-01T00:00:00Z",
        "user": {"id": "user-x", "name": "Ann", "email": "e@x.test"},
        "account": {"planType": "free"}, "cookies": [], "profile_id": "p1",
    }
    info.update(extra)
    return info


class AdapterTestBase(unittest.TestCase):
    EMAIL = "pool@example.test"

    def setUp(self):
        self.released = []   # (email, status, note)
        self.saved = []      # save_account_data 的 kwargs
        self.flow_calls = []  # ant_register_flow 的 kwargs
        self.flow_impl = lambda email, **kw: _flow_info()
        patches = [
            mock.patch.object(email_provider, "acquire_email_after_input",
                              lambda e: str(e or "").strip() or self.EMAIL),
            mock.patch.object(email_provider, "release_email",
                              side_effect=lambda email, status="available", note=None:
                              self.released.append((email, status, note))),
            mock.patch.object(email_provider, "resolve_email_source", lambda e: "outlook"),
            mock.patch.object(account_export, "save_account_data",
                              side_effect=lambda **kw: self.saved.append(kw) or 4242),
            mock.patch.object(bridge, "pick_ant_proxy",
                              lambda explicit=None: explicit or "http://u:p@192.0.2.58:6611"),
            mock.patch.object(bridge, "ant_register_flow", self._flow),
        ]
        self._patches = patches
        for p in patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _flow(self, email, **kw):
        self.flow_calls.append(kw)
        return self.flow_impl(email, **kw)

    def run_driver(self, codex_auto=False, **kw):
        """codex_auto=None 表示不在这一层干预 ENABLE_CODEX_AUTO（由用例自己控制）。"""
        base = dict(email=None, name="Ann", birthday="1995-05-05", proxy=None,
                    otp_code=None, batch_dir=None, on_email_acquired=None)
        base.update(kw)
        if codex_auto is None:
            return bridge.run_ant_registration(**base)
        with mock.patch.object(config_codex(), "ENABLE_CODEX_AUTO", codex_auto):
            return bridge.run_ant_registration(**base)


def config_codex():
    from config import codex
    return codex


class AdapterTest(AdapterTestBase):
    def test_success_saves_account_like_other_drivers(self):
        result = self.run_driver()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["account_id"], 4242)
        self.assertEqual(result["error"], None)
        self.assertEqual(self.released, [], "成功路径不应改动邮箱池状态")
        saved = self.saved[0]
        self.assertEqual(saved["proxy_used"], "http://u:p@192.0.2.58:6611")
        self.assertEqual(saved["extra"]["account"], {"planType": "free"}, "应带上 session.account 供入库取套餐")
        self.assertEqual(saved["extra"]["user"]["id"], "user-x")
        self.assertEqual(saved["extra"]["codex"]["status"], "skipped")
        self.assertTrue(saved["extra"]["registration_password"], "入库时必须带上本轮设置的密码")
        self.assertEqual(result["codex"]["status"], "skipped")

    def test_progress_dict_is_passed_to_flow(self):
        self.run_driver()
        self.assertIn("progress", self.flow_calls[0], "适配层必须把 progress 传给流程函数")
        self.assertIsInstance(self.flow_calls[0]["progress"], dict)

    def test_age_comes_from_birthday(self):
        import datetime
        self.run_driver(birthday="2000-01-01")
        today = datetime.date.today()
        expected = today.year - 2000 - ((today.month, today.day) < (1, 1))
        self.assertEqual(self.flow_calls[0]["age"], expected)
        self.run_driver(birthday=None)
        self.assertIsNone(self.flow_calls[1]["age"], "没有生日时交给流程函数随机")

    def test_explicit_proxy_is_kept(self):
        self.run_driver(proxy="http://u:p@192.0.2.99:1")
        self.assertEqual(self.saved[0]["proxy_used"], "http://u:p@192.0.2.99:1")

    def test_already_registered_disables_email(self):
        def boom(email, **kw):
            raise bridge.EmailAlreadyRegistered("已有账号")
        self.flow_impl = boom
        result = self.run_driver()
        self.assertFalse(result["success"])
        self.assertTrue(result["error"].startswith("EmailAlreadyRegistered:"))
        self.assertEqual([r[1] for r in self.released], ["disabled"])

    def test_unknown_2fa_scraps_email(self):
        def boom(email, **kw):
            raise bridge.EmailHasUnknown2FA("未知 2FA")
        self.flow_impl = boom
        result = self.run_driver()
        self.assertFalse(result["success"])
        self.assertTrue(result["error"].startswith("EmailHasUnknown2FA:"))
        self.assertEqual([r[1] for r in self.released], ["disabled"])
        self.assertTrue(self.released[0][2].startswith("废弃："))

    def test_half_registered_keeps_password_in_note(self):
        def boom(email, **kw):
            raise bridge.RegistrationAdvanced("尾段未完成", password="S3cret-passw0rd")
        self.flow_impl = boom
        result = self.run_driver()
        self.assertFalse(result["success"])
        self.assertEqual(self.released, [(self.EMAIL, "failed",
                                          mock.ANY)])
        self.assertIn("password=S3cret-passw0rd", self.released[0][2], "密码必须留在备注里供接管")
        self.assertTrue(result["error"].startswith("RegistrationAdvanced:"))

    def test_failure_before_password_returns_email_to_pool(self):
        def boom(email, **kw):
            raise RuntimeError("注册：验证码页未出现密码入口")
        self.flow_impl = boom
        result = self.run_driver()
        self.assertFalse(result["success"])
        self.assertEqual([r[1] for r in self.released], ["available"], "未建号，邮箱应退回池子")
        self.assertTrue(result["error"].startswith("RuntimeError:"))

    def test_failure_after_password_is_not_returned_to_pool(self):
        """密码已提交后才失败（如取会话令牌失败）：OpenAI 侧已建号，邮箱不能退回池子。"""
        def boom(email, progress=None, **kw):
            progress["password_submitted"] = True
            raise RuntimeError("注册：未取到会话 token")
        self.flow_impl = boom
        result = self.run_driver()
        self.assertFalse(result["success"])
        self.assertEqual([r[1] for r in self.released], ["failed"], "已建号的邮箱退回池子会白丢一个号")
        self.assertIn("password=", self.released[0][2])
        self.assertTrue(result["error"].startswith("RegistrationAdvanced:"),
                        "调用方据前缀判断邮箱不能当未消耗处理")

    def test_no_proxy_available_returns_email(self):
        def no_proxy(explicit=None):
            raise RuntimeError("Ant 驱动没有可用代理")
        with mock.patch.object(bridge, "pick_ant_proxy", no_proxy):
            result = self.run_driver()
        self.assertFalse(result["success"])
        self.assertEqual([r[1] for r in self.released], ["available"])
        self.assertIn("没有可用代理", result["error"])

    def test_codex_failure_marks_job_failed_but_saves_account(self):
        """与 cloak 驱动一致：ENABLE_CODEX_AUTO 开启且 Codex 失败时，账号仍入库，任务算失败。"""
        fake = {"status": "failed", "ok": False, "message": "boom"}
        with mock.patch.object(config_codex(), "ENABLE_CODEX_AUTO", True), \
                mock.patch("core.codex_oauth.run_codex_oauth", return_value=fake) as codex_mock:
            result = self.run_driver(codex_auto=None)
        codex_mock.assert_called_once()
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Codex 未完成: boom")
        self.assertEqual(result["account_id"], 4242, "Codex 失败不应丢掉已注册的账号")
        self.assertEqual(self.saved[0]["extra"]["codex"], fake)
        self.assertEqual(codex_mock.call_args.kwargs["proxy"], "http://u:p@192.0.2.58:6611")
        self.assertTrue(codex_mock.call_args.kwargs["force"])


class ProxyPickTest(unittest.TestCase):
    POOL = ["http://u:p@203.0.113.10:6611#exit-A", "http://u:p@192.0.2.10:6611#A",
            "http://u:p@198.51.100.20:16611#B", "http://u:p@bad-host:1#C"]

    def test_exclusion_by_substring(self):
        from config import antbrowser
        with mock.patch.object(proxy_cfg, "PROXY_POOL", list(self.POOL)), \
                mock.patch.object(antbrowser, "ANT_PROXY_EXCLUDE", "203.0.113.10, bad-host"):
            self.assertEqual(bridge.ant_proxy_candidates(), self.POOL[1:3])
            picked = {bridge.pick_ant_proxy() for _ in range(300)}
            self.assertEqual(picked, set(self.POOL[1:3]), "抽 300 次都不应抽到被排除的出口")

    def test_no_exclusion_uses_whole_pool(self):
        from config import antbrowser
        with mock.patch.object(proxy_cfg, "PROXY_POOL", list(self.POOL)), \
                mock.patch.object(antbrowser, "ANT_PROXY_EXCLUDE", ""):
            self.assertEqual(bridge.ant_proxy_candidates(), self.POOL)

    def test_explicit_proxy_not_filtered(self):
        from config import antbrowser
        with mock.patch.object(antbrowser, "ANT_PROXY_EXCLUDE", "203.0.113.10"):
            self.assertEqual(bridge.pick_ant_proxy(self.POOL[0]), self.POOL[0])

    def test_all_excluded_raises(self):
        from config import antbrowser
        with mock.patch.object(proxy_cfg, "PROXY_POOL", list(self.POOL)), \
                mock.patch.object(antbrowser, "ANT_PROXY_EXCLUDE", "u:p@"):  # 四条都带认证串 → 全部排除
            self.assertEqual(bridge.ant_proxy_candidates(), [])
            with self.assertRaises(RuntimeError):
                bridge.pick_ant_proxy()


class FakePage:
    def __init__(self):
        self.url = "https://auth.openai.com/log-in"
        self.session = None
        self.evaluated = []
        self.body_text = ""
        # 登录流程用内联 evaluate 探测元素是否可见，替身按这三个开关回答
        self.email_visible = True
        self.pwd_visible = False
        self.otp_visible = False

    def goto(self, url, **kw):
        self.url = url

    def evaluate(self, script, *args):
        self.evaluated.append(script)
        if "/api/auth/session" in script:
            return self.session
        if "input[type='password']" in script:
            return 0
        if "innerText" in script:
            return self.body_text
        css = str(args[0]) if args else ""
        if ".some(v)" in script:                      # 邮箱输入框探测
            return self.email_visible
        if "els.length ? true : false" in script or ".some(visible)" in script:
            return self.otp_visible if _is_otp_css(css) else self.pwd_visible
        return {}

    def screenshot(self, **kw):
        pass


class FlowProgressTest(unittest.TestCase):
    """ant_register_flow 的进度标记：密码提交成功才置位；尾段失败抛 RegistrationAdvanced 并带密码。"""

    def _run(self, tail_url, session=None, with_progress=True, route="link", birthdate_dialog=False):
        page = FakePage()
        page.session = session
        clock = [1000.0]
        state = {"dialog_open": False, "confirmed": 0}
        self._last_state = state

        def fake_time():
            clock[0] += 1.0    # 看一次表前进 1s：长等待循环几百次就到超时，短循环不会一步跨过
            return clock[0]

        submits = []

        def human_submit(pg, selectors, *a, **kw):
            submits.append(1)
            if len(submits) > 2 and birthdate_dialog:   # 资料页点「继续」→ 弹出生日期确认框，地址不变
                state["dialog_open"] = True
            elif len(submits) == 2:          # 密码提交后 → 尾段
                page.url = tail_url
            elif route == "direct":       # 提交邮箱后直达创建密码页
                page.url = "https://auth.openai.com/create-account/password?auth_page=x"
            elif route == "stuck":        # 提交邮箱后停在既无密码入口、也非密码页的页面
                page.url = "https://auth.openai.com/log-in/identifier"
            else:                         # 常规：验证码页
                page.url = "https://auth.openai.com/email-verification?auth_page=x"

        def click_pw(pg, patterns, *a, **kw):
            if patterns is bridge._BIRTHDATE_CONFIRM_PATTERNS:
                if not state["dialog_open"]:
                    return ""              # 没有确认框时不能乱点（「继续」「取消」都不匹配）
                state["dialog_open"] = False
                state["confirmed"] += 1
                page.url = "https://chatgpt.com/"
                return "button:确定"
            if route != "link":
                return ""              # 页面上没有「使用密码继续」可点
            page.url = "https://auth.openai.com/create-account/password"
            return "a:使用密码继续"

        @contextlib.contextmanager
        def fake_ant_page(proxy_url, email=None):
            yield page, object(), mock.Mock(profile_id="p1")

        ctx = contextlib.ExitStack()
        ctx.enter_context(mock.patch.object(bridge, "_ant_page", fake_ant_page))
        ctx.enter_context(mock.patch.object(bridge, "_visible_css", lambda pg, lst: "css"))
        ctx.enter_context(mock.patch.object(bridge, "_human_type", lambda pg, css, text: True))
        ctx.enter_context(mock.patch.object(bridge, "_human_submit", human_submit))
        ctx.enter_context(mock.patch.object(bridge, "_force_click_by_text", click_pw))
        ctx.enter_context(mock.patch.object(bridge, "_detect_challenge", lambda pg: {"active": False}))
        for name in ("_wait_out_challenge", "_heal_chrome_error"):
            ctx.enter_context(mock.patch.object(bridge, name, lambda *a, **kw: None))
        ctx.enter_context(mock.patch.object(bridge, "_export_cookies", lambda ctx_obj: []))
        ctx.enter_context(mock.patch("time.time", fake_time))
        ctx.enter_context(mock.patch("time.sleep", lambda s: None))
        progress: dict = {}
        kwargs = {"progress": progress} if with_progress else {}
        result = exc = None
        with ctx:
            try:
                result = bridge.ant_register_flow("e@x.test", proxy="http://u:p@192.0.2.10:1", password="Pw12345678",
                                                  full_name="Ann", age=30, **kwargs)
            except Exception as e:  # noqa: BLE001
                exc = e
        return result, exc, progress

    def test_tail_failure_is_half_registered_with_password(self):
        result, exc, progress = self._run("https://auth.openai.com/somewhere-unknown")
        self.assertIsNone(result)
        self.assertIsInstance(exc, bridge.RegistrationAdvanced)
        self.assertEqual(getattr(exc, "password", None), "Pw12345678")
        self.assertEqual(progress, {"password_submitted": True})

    def test_success_returns_session_objects(self):
        session = {"token": "tok", "uid": "user-1", "name": "Ann", "expires": "E",
                   "user": {"id": "user-1"}, "account": {"planType": "free"}}
        result, exc, progress = self._run("https://chatgpt.com/", session=session)
        self.assertIsNone(exc)
        self.assertEqual(result["account"], {"planType": "free"}, "成功时应带回 session.account")
        self.assertEqual(result["user"]["id"], "user-1")
        self.assertEqual(result["access_token"], "tok")
        self.assertEqual(progress, {"password_submitted": True})

    def test_no_progress_dict_is_also_fine(self):
        """不传 progress 的调用方（旧脚本）不受影响：参数是可选的。"""
        _result, exc, _progress = self._run("https://auth.openai.com/somewhere-unknown", with_progress=False)
        self.assertIsInstance(exc, bridge.RegistrationAdvanced)
        self.assertEqual(getattr(exc, "password", None), "Pw12345678")

    def test_direct_to_create_password_page_is_accepted(self):
        """回归（2026-11-23 任务 #217）：提交邮箱后不经验证码页直达创建密码页，必须继续设密码。

        修复前流程只认「点到使用密码继续」，在这条路由上干等 420s 后报
        「验证码页未出现密码入口」，把一步之遥的注册判成失败。
        """
        session = {"token": "tok", "uid": "user-1", "name": "Ann", "expires": "E",
                   "user": {"id": "user-1"}, "account": {"planType": "free"}}
        result, exc, progress = self._run("https://chatgpt.com/", session=session, route="direct")
        self.assertIsNone(exc, "直达创建密码页不应被当成失败：%r" % (exc,))
        self.assertEqual(result["access_token"], "tok")
        self.assertEqual(progress, {"password_submitted": True})

    def test_neither_link_nor_password_page_times_out_before_password(self):
        """既没有密码入口也没到密码页：超时失败，且进度标记不能被置位（邮箱应退回池子）。"""
        _result, exc, progress = self._run("about:blank", route="stuck")
        self.assertIsInstance(exc, RuntimeError)
        self.assertNotIsInstance(exc, bridge.RegistrationAdvanced)
        self.assertIn("验证码页未出现密码入口", str(exc))
        self.assertEqual(progress, {})

    def test_birthdate_confirm_dialog_is_accepted(self):
        """回归（2026-11-23 任务 #218，年龄 21）：资料页点「继续」后弹「你正在将出生日期设置为…」确认框。

        必须点「确定」。修复前流程只会反复填表、点继续，直到尾段超时，
        把一个已经建好并验证过邮箱的号判成半注册。
        """
        session = {"token": "tok", "uid": "user-1", "name": "Ann", "expires": "E",
                   "user": {"id": "user-1"}, "account": {"planType": "free"}}
        result, exc, progress = self._run("https://auth.openai.com/about-you?auth_page=x",
                                          session=session, route="direct", birthdate_dialog=True)
        self.assertIsNone(exc, "出生日期确认框不应卡住流程：%r" % (exc,))
        self.assertEqual(self._last_state["confirmed"], 1, "确认框应恰好点一次")
        self.assertEqual(result["access_token"], "tok")
        self.assertEqual(progress, {"password_submitted": True})

    def test_without_dialog_there_is_no_stray_confirm_click(self):
        """资料页没有确认框时不能乱点（「继续」「取消」都不该被当成确认）。"""
        session = {"token": "tok", "uid": "user-1", "name": "Ann", "expires": "E",
                   "user": {"id": "user-1"}, "account": {"planType": "free"}}
        result, exc, _progress = self._run("https://chatgpt.com/", session=session, route="direct")
        self.assertIsNone(exc)
        self.assertEqual(self._last_state["confirmed"], 0)

    def test_confirm_pattern_is_exact_match(self):
        """确认框匹配必须是整词：含「确定」的其它文案、以及「继续」「取消」都不能命中。"""
        import re
        rx = re.compile(bridge._BIRTHDATE_CONFIRM_PATTERNS[0], re.I)
        self.assertTrue(rx.search("确定"))
        self.assertTrue(rx.search("Confirm"))
        self.assertFalse(rx.search("继续"))
        self.assertFalse(rx.search("取消"))
        self.assertFalse(rx.search("确定并继续注册"))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        self.now += 1.0
        return self.now


class AutoRegisterLogicTest(unittest.TestCase):
    """自动化模块里不依赖外部资源的小函数。"""

    def test_failure_classification(self):
        self.assertEqual(auto_reg._classify_failure("RegistrationAdvanced: x")[0], "half_registered")
        self.assertEqual(auto_reg._classify_failure("EmailAlreadyRegistered: x")[0], "already_registered")
        self.assertEqual(auto_reg._classify_failure("EmailHasUnknown2FA: x")[0], "unknown_2fa")
        self.assertEqual(auto_reg._classify_failure("RuntimeError: y")[1], "邮箱已退回池子")

    def test_proxy_exit_from_log(self):
        import tempfile
        import os
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("a\n[Ant注册] x 本轮代理出口：exit-1.example:16611\nb\n")
        try:
            self.assertEqual(auto_reg._proxy_exit_from_log({"log_file": path}), "exit-1.example:16611")
            self.assertEqual(auto_reg._proxy_exit_from_log({"log_file": path + ".missing"}), "")
        finally:
            os.unlink(path)

    def test_wait_twofa_not_queued_when_status_never_appears(self):
        import core.db as db
        with mock.patch.object(db, "get_account", return_value={"totp_setup_status": None}), \
                mock.patch("time.sleep", lambda s: None), \
                mock.patch.object(auto_reg.time, "time", side_effect=FakeClock().time):
            self.assertEqual(auto_reg.wait_twofa(1, queue_wait=5, timeout=5)["state"], "not_queued")

    def test_wait_twofa_success(self):
        import core.db as db
        rows = iter([{"totp_setup_status": "queued"}, {"totp_setup_status": "running"},
                     {"totp_setup_status": "success", "totp_secret": "ABC"}])
        with mock.patch.object(db, "get_account", side_effect=lambda aid: next(rows)), \
                mock.patch("time.sleep", lambda s: None), \
                mock.patch.object(auto_reg.time, "time", side_effect=FakeClock().time):
            self.assertEqual(auto_reg.wait_twofa(1, queue_wait=50, timeout=50)["state"], "success")

    def test_wait_twofa_failure_carries_error(self):
        import core.db as db
        rows = iter([{"totp_setup_status": "running"}, {"totp_setup_status": "failed", "totp_secret": ""}])
        with mock.patch.object(db, "get_account", side_effect=lambda aid: next(rows)), \
                mock.patch("time.sleep", lambda s: None), \
                mock.patch.object(auto_reg.time, "time", side_effect=FakeClock().time):
            out = auto_reg.wait_twofa(1, queue_wait=50, timeout=50)
        self.assertEqual(out["state"], "failed")


class NodeStateTest(unittest.TestCase):
    """节点/队列状态判断与预检。

    节点客户端的替身用 spec=真实客户端类：本模块若调用了真实客户端上不存在的方法，
    测试会直接 AttributeError（2026-11-23 就是这么漏过一个 runtime_active → 应为 request/active_profile）。
    """

    def _fake_client(self, *, healthy=True, active=False, raise_on=None):
        import core.ant_browser_client as ant_client
        fake = mock.Mock(spec=ant_client.AntBrowserClient)
        fake.api_base = "http://ant.invalid:19876"  # 真实客户端在 __init__ 里设置的实例属性
        fake.health.return_value = healthy
        if raise_on is not None:
            fake.request.side_effect = raise_on
        else:
            fake.request.return_value = {"active": active, "profileName": "manual-155-r2", "ok": True}
        return ant_client, fake

    @staticmethod
    def _db_patches(active_jobs=0, pending_twofa=0):
        import core.db as db
        return [
            mock.patch.object(db, "job_status_counts", return_value={"active": active_jobs}),
            mock.patch.object(db, "totp_setup_activity",
                              return_value={"pending": pending_twofa, "pending_since": None}),
        ]

    def _blocker(self, fake, **kw):
        import core.ant_browser_client as ant_client
        with contextlib.ExitStack() as ctx:
            for p in self._db_patches(**kw):
                ctx.enter_context(p)
            ctx.enter_context(mock.patch.object(ant_client, "AntBrowserClient", lambda: fake))
            return auto_reg._idle_blocker()

    def test_idle_when_node_free(self):
        _mod, fake = self._fake_client()
        self.assertIsNone(self._blocker(fake))

    def test_busy_when_instance_running_on_node(self):
        _mod, fake = self._fake_client(active=True)
        self.assertEqual(self._blocker(fake), "节点上有其它实例在运行")

    def test_query_failure_is_not_treated_as_idle(self):
        _mod, fake = self._fake_client(raise_on=RuntimeError("tunnel down"))
        self.assertTrue(self._blocker(fake).startswith("节点状态查询失败"))

    def test_active_job_blocks_without_touching_node(self):
        _mod, fake = self._fake_client()
        self.assertEqual(self._blocker(fake, active_jobs=2), "2 个注册任务未结束")
        fake.request.assert_not_called()

    def test_pending_twofa_blocks(self):
        _mod, fake = self._fake_client()
        self.assertEqual(self._blocker(fake, pending_twofa=1), "1 个 2FA 进行中")

    def _preflight(self, fake, **kw):
        import core.ant_browser_client as ant_client
        import core.auto_register_scheduler as scheduler
        from config import roxybrowser as roxy_cfg
        driver = kw.pop("driver", "ant")
        with contextlib.ExitStack() as ctx:
            for p in self._db_patches():
                ctx.enter_context(p)
            ctx.enter_context(mock.patch.object(scheduler, "available_emails", return_value=5))
            ctx.enter_context(mock.patch.object(bridge, "ant_proxy_candidates", return_value=["a", "b"]))
            ctx.enter_context(mock.patch.object(proxy_cfg, "PROXY_POOL", ["a", "b", "c"]))
            ctx.enter_context(mock.patch.object(roxy_cfg, "REGISTRATION_DRIVER", driver))
            ctx.enter_context(mock.patch.object(auto_reg, "_webui", return_value=(200, {})))
            ctx.enter_context(mock.patch.object(ant_client, "AntBrowserClient", lambda: fake))
            return {c["name"]: c for c in auto_reg.collect_preflight()}

    def test_preflight_all_ok(self):
        _mod, fake = self._fake_client()
        checks = self._preflight(fake)
        self.assertEqual([n for n, c in checks.items() if not c["ok"]], [])
        self.assertIn("HK 节点空闲", checks)
        self.assertEqual(checks["有节点可达的代理出口"]["detail"], "2 / 3 条可用")

    def test_preflight_node_busy_is_waitable_not_fatal(self):
        _mod, fake = self._fake_client(active=True)
        checks = self._preflight(fake)
        self.assertFalse(checks["HK 节点空闲"]["ok"])
        self.assertIn("manual-155-r2", checks["HK 节点空闲"]["detail"])
        self.assertTrue(checks["HK 节点 Ant Browser 在线"]["ok"])
        hard = [n for n, c in checks.items() if not c["ok"] and n not in auto_reg._WAITABLE]
        self.assertEqual(hard, [], "节点被占用等一等即可，不该当成致命错误")

    def test_preflight_node_down_is_fatal(self):
        _mod, fake = self._fake_client(healthy=False)
        checks = self._preflight(fake)
        hard = [n for n, c in checks.items() if not c["ok"] and n not in auto_reg._WAITABLE]
        self.assertEqual(hard, ["HK 节点 Ant Browser 在线"])

    def test_preflight_wrong_driver_is_fatal(self):
        _mod, fake = self._fake_client()
        checks = self._preflight(fake, driver="cloak")
        self.assertFalse(checks["注册驱动为 ant"]["ok"])


class CodexDriverResolutionTest(unittest.TestCase):
    """Codex 授权驱动解析：显式 cloak 走 CloakBrowser 分支；same_as_registration + ant 是死路。"""

    def test_cloak_driver_branch(self):
        import core.codex_oauth as codex_oauth
        import core.roxy_codex_oauth as roxy_codex
        from config import codex as codex_cfg
        sentinel = {"status": "success", "ok": True}
        fake_driver = mock.Mock()
        with mock.patch.object(codex_cfg, "ENABLE_CODEX_AUTO", True), \
                mock.patch.object(codex_cfg, "CODEX_OAUTH_DRIVER", "cloak"), \
                mock.patch("core.cloakbrowser_driver.build_cloak_driver", return_value=(fake_driver, mock.Mock())) as build, \
                mock.patch.object(roxy_codex, "run_roxy_codex_oauth", return_value=sentinel) as run:
            out = codex_oauth.run_codex_oauth("nobody@example.invalid", proxy="http://u:p@192.0.2.10:1", force=True)
        self.assertIs(out, sentinel)
        build.assert_called_once()
        self.assertTrue(run.call_args.kwargs["force"])

    def test_same_as_registration_with_ant_driver_is_unsupported(self):
        import core.codex_oauth as codex_oauth
        from config import codex as codex_cfg
        from config import roxybrowser as roxy_cfg
        with mock.patch.object(codex_cfg, "ENABLE_CODEX_AUTO", True), \
                mock.patch.object(codex_cfg, "CODEX_OAUTH_DRIVER", "same_as_registration"), \
                mock.patch.object(roxy_cfg, "REGISTRATION_DRIVER", "ant"):
            with self.assertRaises(RuntimeError) as cm:
                codex_oauth.run_codex_oauth("nobody@example.invalid", force=True)
        self.assertIn("不支持的 CODEX_OAUTH_DRIVER", str(cm.exception))


class FlowHarness:
    """ant_register_flow 的通用替身环境：假页面 + 假时钟 + 可编排的提交/点击/取码回调。

    用法:
        with FlowHarness(session) as h:
            h.on_submit = ...   # 每次 _human_submit 之后改 page.url，模拟页面走向
            h.on_click = ...    # _force_click_by_text(patterns)
            h.on_otp = ...      # _handle_create_otp(page, email, after_ts, **kw)
            result = h.run(takeover="password")
    看一次表前进 1s；所有 sleep 都是空操作。
    """

    PW = "Pw12345678"
    SESSION = {"token": "tok", "uid": "user-1", "name": "Ann", "expires": "E",
               "user": {"id": "user-1"}, "account": {"planType": "free"}}
    ENTRY = "https://chatgpt.com/auth/login"                                     # 注册和接管共用的入口
    LOGIN_FORM = "https://chatgpt.com/auth/login_with?callback_path=/"
    SESSION_EXPIRED = "https://auth.openai.com/log-in?error=expired"
    LOGIN = "https://auth.openai.com/log-in"
    VERIFY_LINK = "https://auth.openai.com/email-verification?auth_page=L"    # 带「使用密码继续」
    VERIFY_CONFIRM = "https://auth.openai.com/email-verification?auth_page=C"  # 创建账户确认
    LOGIN_PWD = "https://auth.openai.com/log-in/password"
    CREATE_PWD = "https://auth.openai.com/create-account/password"
    ABOUT_YOU = "https://auth.openai.com/about-you?auth_page=x"
    HOME = "https://chatgpt.com/"

    def __init__(self, session=None):
        self.page = FakePage()
        self.page.session = session
        self.clock = [1000.0]
        self.submits = 0
        self.typed = []        # [(css, text)]
        self.otp_calls = []    # [(after_ts, kwargs)]
        self.goto_urls = []
        self.email_submit_clock = None
        self.name_input = False   # 页面上有没有姓名输入框（只有资料页有）
        self.email_input = True   # 页面上有没有邮箱输入框（会话失效页之类没有）
        self.retry_clicks = 0     # 「会话已失效 → 重试」被点了几次
        self.on_submit = lambda h: None
        self.on_click = lambda h, patterns: ""
        self.on_otp = lambda h, after_ts, kw: True
        self.on_entry = lambda h: h.go(h.ENTRY)              # 打开入口后落到哪一页
        self.on_session_retry = lambda h: ""

    def time(self):
        self.clock[0] += 1.0
        return self.clock[0]

    def _goto(self, url, **kw):
        self.goto_urls.append(url)
        if url == self.ENTRY:
            self.on_entry(self)
        else:
            self.go(url)

    def go(self, url, *, email_input=None, name_input=None):
        """页面导航：未显式指定时，姓名输入框只在资料页可见，邮箱输入框保持原状。"""
        self.page.url = url
        if email_input is not None:
            self.email_input = email_input
        if name_input is None:
            name_input = "about-you" in url
        self.name_input = name_input

    def _visible(self, pg, css_list):
        """替身的「元素是否可见」：邮箱/姓名输入框按标志回答，其余一律可见。"""
        if css_list is bridge._NAME_INPUT and not self.name_input:
            return None
        if css_list is bridge._EMAIL_INPUT_SELECTORS and not self.email_input:
            return None
        return "css"

    def _submit(self, pg, selectors, *a, **kw):
        self.submits += 1
        if self.submits == 1:
            self.email_submit_clock = self.clock[0]   # 与流程里的 email_submit_ts 是同一次看表
        self.on_submit(self)

    def _click(self, pg, patterns, *a, **kw):
        if patterns is bridge._BIRTHDATE_CONFIRM_PATTERNS:
            return ""                                   # 没有出生日期确认框
        if patterns is bridge._SESSION_RETRY_PATTERNS:
            self.retry_clicks += 1
            return self.on_session_retry(self)
        return self.on_click(self, patterns)

    def _otp(self, pg, email, after_ts, **kw):
        self.otp_calls.append((after_ts, dict(kw)))
        return self.on_otp(self, after_ts, kw)

    def __enter__(self):
        page = self.page
        page.goto = self._goto
        self._ctx = ctx = contextlib.ExitStack()

        @contextlib.contextmanager
        def fake_ant_page(proxy_url, email=None):
            yield page, object(), mock.Mock(profile_id="p1")

        ctx.enter_context(mock.patch.object(bridge, "_ant_page", fake_ant_page))
        ctx.enter_context(mock.patch.object(bridge, "_visible_css", self._visible))
        ctx.enter_context(mock.patch.object(
            bridge, "_human_type", lambda pg, css, text: self.typed.append((css, text)) or True))
        ctx.enter_context(mock.patch.object(bridge, "_human_submit", self._submit))
        ctx.enter_context(mock.patch.object(bridge, "_force_click_by_text", self._click))
        ctx.enter_context(mock.patch.object(bridge, "_click_entry_button", lambda pg: False))
        ctx.enter_context(mock.patch.object(bridge, "_detect_challenge", lambda pg: {"active": False}))
        for name in ("_wait_out_challenge", "_heal_chrome_error"):
            ctx.enter_context(mock.patch.object(bridge, name, lambda *a, **kw: None))
        ctx.enter_context(mock.patch.object(bridge, "_export_cookies", lambda c: []))
        ctx.enter_context(mock.patch.object(bridge, "_handle_create_otp", self._otp))
        ctx.enter_context(mock.patch("time.time", self.time))
        ctx.enter_context(mock.patch("time.sleep", lambda s: None))
        return self

    def __exit__(self, *exc):
        self._ctx.__exit__(*exc)
        return False

    def run(self, **kw):
        kw.setdefault("proxy", "http://u:p@192.0.2.10:1")
        kw.setdefault("password", self.PW)
        kw.setdefault("full_name", "Ann")
        kw.setdefault("age", 30)
        return bridge.ant_register_flow("e@x.test", **kw)

    # ---- 常用编排 ----
    def password_typed(self):
        return ("css", self.PW) in self.typed


def _after_submit_password_login(h):
    """接管（密码登录）每次提交后的页面走向：邮箱 → 带密码入口的验证码页；
    密码 → 创建账户确认验证码页；资料页 → 回站。"""
    if h.submits == 1:
        h.go(h.VERIFY_LINK)
    elif h.submits == 2:
        h.go(h.VERIFY_CONFIRM)
    else:
        h.go(h.HOME)


def _after_submit_otp_login(h):
    """接管（一次性验证码登录，不输密码）：邮箱 → 验证码页；资料页 → 回站。"""
    if h.submits == 1:
        h.go(h.VERIFY_LINK)
    else:
        h.go(h.HOME)


def _click_pw_link(h, patterns):
    """验证码页上有「使用密码继续」，点了进登录密码页。"""
    if h.page.url == h.VERIFY_LINK:
        h.go(h.LOGIN_PWD)
        return "a:使用密码继续"
    return ""


def _click_pw_link_register(h, patterns):
    """注册流程：验证码页上点「使用密码继续」进创建密码页。"""
    if h.page.url == h.VERIFY_LINK:
        h.go(h.CREATE_PWD)
        return "a:使用密码继续"
    return ""


def _otp_then_about_you(h, after_ts, kw):
    h.go(h.ABOUT_YOU)
    return True


class TakeoverFlowTest(unittest.TestCase):
    """接管半注册账号：从登录页用已有密码（或一次性验证码）登录，再走注册的后半程。"""

    def test_password_login_completes_pending_account(self):
        with FlowHarness(FlowHarness.SESSION) as h:
            h.on_submit, h.on_click, h.on_otp = _after_submit_password_login, _click_pw_link, _otp_then_about_you
            progress = {}
            result = h.run(takeover="password", progress=progress)
        self.assertEqual(result["access_token"], "tok")
        self.assertEqual(result["account"], {"planType": "free"})
        self.assertEqual(h.goto_urls, [FlowHarness.ENTRY],
                         "注册和接管用同一个入口：直开登录页会落到「会话已失效」")
        self.assertTrue(h.password_typed(), "要用已有密码登录")
        self.assertEqual(len(h.otp_calls), 1, "密码登录后只需一轮创建账户确认验证码")
        self.assertNotIn("label", h.otp_calls[0][1], "第 1 轮沿用默认的取码参数")
        self.assertGreater(h.otp_calls[0][0], h.email_submit_clock, "确认验证码的时间下限应在密码提交之后")
        self.assertEqual(progress, {}, "接管模式不该动 progress")

    def test_otp_login_without_password(self):
        """没有密码记录：验证码页就是登录入口，不点「使用密码继续」，也不敲密码。"""
        with FlowHarness(FlowHarness.SESSION) as h:
            h.on_submit, h.on_otp = _after_submit_otp_login, _otp_then_about_you
            result = h.run(takeover="otp", password="")
        self.assertEqual(result["access_token"], "tok")
        self.assertFalse(h.password_typed())
        self.assertEqual(len(h.otp_calls), 1)
        self.assertEqual(h.otp_calls[0][0], h.email_submit_clock,
                         "登录验证码在提交邮箱时就发出了，时间下限必须是那一刻")

    def test_falls_back_to_otp_when_password_link_never_shows(self):
        """有密码但 20s 内等不到「使用密码继续」：改走一次性验证码登录。"""
        with FlowHarness(FlowHarness.SESSION) as h:
            h.on_submit, h.on_otp = _after_submit_otp_login, _otp_then_about_you
            result = h.run(takeover="password")
        self.assertEqual(result["access_token"], "tok")
        self.assertFalse(h.password_typed())
        self.assertEqual(h.otp_calls[0][0], h.email_submit_clock)

    def test_direct_login_password_page_is_accepted(self):
        """提交邮箱后直接落到登录密码页：接管模式照常输密码，不判「已有账号」。"""
        with FlowHarness(FlowHarness.SESSION) as h:
            def after(h_):
                if h_.submits == 1:
                    h_.go(h_.LOGIN_PWD)
                elif h_.submits == 2:
                    h_.go(h_.ABOUT_YOU)      # 这个账号不再验邮箱，直接到资料页
                else:
                    h_.go(h_.HOME)
            h.on_submit, h.on_otp = after, _otp_then_about_you
            result = h.run(takeover="password")
        self.assertEqual(result["access_token"], "tok")
        self.assertEqual(h.otp_calls, [], "这个账号不需要再验邮箱")
        self.assertTrue(h.password_typed())

    def test_already_logged_in_needs_no_password(self):
        with FlowHarness(FlowHarness.SESSION) as h:
            h.on_submit = lambda h_: h_.go(h_.HOME)
            result = h.run(takeover="otp", password="")
        self.assertEqual(result["access_token"], "tok")
        self.assertEqual((h.otp_calls, h.password_typed()), ([], False))

    def test_wrong_password_fails_fast(self):
        """密码提交后还停在密码页：12s 内判定失败，不干等到尾段超时。"""
        with FlowHarness(FlowHarness.SESSION) as h:
            def after(h_):
                if h_.submits == 1:
                    h_.go(h_.VERIFY_LINK)
                # 密码提交后页面不动 = 密码没被接受
            h.on_submit, h.on_click = after, _click_pw_link
            with self.assertRaises(bridge.TakeoverLoginFailed) as cm:
                h.run(takeover="password")
        self.assertIn("密码未被接受", str(cm.exception))
        self.assertLess(h.clock[0] - 1000.0, 120, "应在几十秒（假时钟）内失败")

    def test_login_that_never_completes_reports_login_failure(self):
        with FlowHarness(FlowHarness.SESSION) as h:
            h.on_submit = lambda h_: h_.go("https://auth.openai.com/somewhere-unknown")
            with self.assertRaises(bridge.TakeoverLoginFailed):
                h.run(takeover="otp", password="")

    def test_argument_validation(self):
        with self.assertRaises(ValueError):
            bridge.ant_register_flow("e@x.test", proxy="p", takeover="password")     # 没给密码
        with self.assertRaises(ValueError):
            bridge.ant_register_flow("e@x.test", proxy="p", password="x", takeover="nope")

    def test_registration_still_rejects_existing_account(self):
        """常规注册没变：新邮箱提交后直达登录密码页仍然判「已有账号」。"""
        with FlowHarness(FlowHarness.SESSION) as h:
            h.on_submit = lambda h_: h_.go(h_.LOGIN_PWD)
            with self.assertRaises(bridge.EmailAlreadyRegistered):
                h.run()
        self.assertEqual(h.goto_urls, ["https://chatgpt.com/auth/login"])

    # ---- 入口落到「会话已失效」页（2026-11-24 首次接管实测） ----

    @staticmethod
    def _land_on_expired_session(h):
        def on_entry(h_):
            h_.go(h_.SESSION_EXPIRED, email_input=False)
            h_.page.body_text = "会话已失效 您的登录会话已失效。请重新开始以继续。 重试 诊断信息 复制诊断信息"

        def on_retry(h_):
            h_.go(h_.LOGIN_FORM, email_input=True)
            h_.page.body_text = "欢迎回来 电子邮件地址 继续"
            return "button:重试"

        h.on_entry, h.on_session_retry = on_entry, on_retry

    def test_expired_login_session_is_retried(self):
        """回归（2026-11-24 首次接管实测）：落到「会话已失效（invalid_state）」页，点「重试」后接着走完。"""
        with FlowHarness(FlowHarness.SESSION) as h:
            self._land_on_expired_session(h)
            h.on_submit, h.on_click, h.on_otp = _after_submit_password_login, _click_pw_link, _otp_then_about_you
            result = h.run(takeover="password")
        self.assertEqual(result["access_token"], "tok")
        self.assertEqual(h.retry_clicks, 1)
        self.assertEqual(h.goto_urls, [FlowHarness.ENTRY], "重试是页内点击，不该重新打开入口")

    def test_retry_button_is_not_clicked_unless_the_session_really_expired(self):
        """别的页面上也有「重试」按钮：页面文字不是会话失效就不能点。"""
        with FlowHarness(FlowHarness.SESSION) as h:
            def on_entry(h_):
                h_.go("https://chatgpt.com/some-error", email_input=False)
                h_.page.body_text = "出错了 重试"

            h.on_entry = on_entry
            with self.assertRaises(RuntimeError) as cm:
                h.run(takeover="password")
        self.assertIn("邮箱输入未就绪", str(cm.exception))
        self.assertEqual(h.retry_clicks, 0)

    def test_repeatedly_expired_session_fails_fast(self):
        with FlowHarness(FlowHarness.SESSION) as h:
            def on_entry(h_):
                h_.go(h_.SESSION_EXPIRED, email_input=False)
                h_.page.body_text = "会话已失效 您的登录会话已失效。请重新开始以继续。 重试"

            h.on_entry = on_entry
            h.on_session_retry = lambda h_: "button:重试"        # 点了还是停在这一页
            with self.assertRaises(RuntimeError) as cm:
                h.run(takeover="password")
        self.assertIn("反复失效", str(cm.exception))
        self.assertEqual(h.retry_clicks, 2, "最多重试两次")
        self.assertLess(h.clock[0] - 1000.0, 200, "不该干等到入口超时")
class OtpRoundsTest(unittest.TestCase):
    """同一次流程里连要多轮验证码，以及尾段循环不能吞掉终局异常。"""

    def _run(self, on_otp, tail_after_password=FlowHarness.VERIFY_CONFIRM):
        with FlowHarness(FlowHarness.SESSION) as h:
            def after(h_):
                if h_.submits == 1:
                    h_.page.url = h_.VERIFY_LINK
                elif h_.submits == 2:
                    h_.page.url = tail_after_password
                else:
                    h_.page.url = h_.HOME
            h.on_submit, h.on_click, h.on_otp = after, _click_pw_link_register, on_otp
            exc = None
            try:
                h.run()
            except Exception as e:  # noqa: BLE001
                exc = e
        return h, exc

    def test_single_round(self):
        h, exc = self._run(_otp_then_about_you)
        self.assertIsNone(exc)
        self.assertEqual(len(h.otp_calls), 1)
        self.assertNotIn("label", h.otp_calls[0][1], "第 1 轮沿用原来的取码参数")

    def test_second_round_uses_only_mails_after_first_submit(self):
        calls = {"n": 0}

        def on_otp(h, after_ts, kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return True                 # 第 1 轮提交后页面停在验证码页，再要一轮
            h.go(h.ABOUT_YOU)
            return True

        h, exc = self._run(on_otp)
        self.assertIsNone(exc)
        self.assertEqual(len(h.otp_calls), 2)
        second = h.otp_calls[1]
        self.assertEqual(second[1].get("label"), "第 2 轮")
        self.assertEqual((second[1].get("max_wait"), second[1].get("attempts"), second[1].get("skew")), (120, 1, 3))
        self.assertGreaterEqual(second[0], h.otp_calls[0][0], "第 2 轮的时间下限是第 1 轮的提交时刻")

    def test_second_round_without_new_mail_fails_fast(self):
        calls = {"n": 0}

        def on_otp(h, after_ts, kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return True
            raise RuntimeError("注册：未取到第 2 轮验证码")

        h, exc = self._run(on_otp)
        self.assertIsInstance(exc, bridge.OtpRoundFailed, "终局异常必须冒泡，不能被尾段兜底吞掉：%r" % (exc,))

    def test_first_round_failure_retries_the_same_round(self):
        """第 1 轮没取到码：保持原状重试同一轮（时间下限和参数都不变），不升级成第 2 轮。"""
        calls = {"n": 0}

        def on_otp(h, after_ts, kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("注册：未取到创建账户验证码")
            h.page.url = h.HOME
            return True

        h, exc = self._run(on_otp)
        self.assertIsNone(exc)
        self.assertEqual(len(h.otp_calls), 2)
        self.assertEqual(h.otp_calls[0], h.otp_calls[1])

    def test_unknown_2fa_page_is_not_swallowed(self):
        """回归：终局异常以前被尾段的兜底 except 吞成一条 debug 日志，「撞上未知 2FA 就废弃邮箱」从未生效。"""
        h, exc = self._run(_otp_then_about_you, tail_after_password="https://auth.openai.com/mfa-challenge")
        self.assertIsInstance(exc, bridge.EmailHasUnknown2FA, "%r" % (exc,))


class TakeoverLogicTest(unittest.TestCase):
    """自动化模块的接管编排：列清单、单个接管、选目标。"""

    ROWS = [
        {"id": 134, "email": "a@x.test", "status": "failed",
         "note": "半注册：密码已设但本轮密码未落日志（旧代码），OpenAI 侧已有账号"},
        {"id": 136, "email": "b@x.test", "status": "failed",
         "note": "半注册待补密码接管 password=Fk9!ppWw3/Qzr7；注册已过密码页但未完成"},
        {"id": 139, "email": "c@x.test", "status": "failed",
         "note": "已建号但流程未走完 password=Xyz^1；注册已过密码页"},
        {"id": 140, "email": "d@x.test", "status": "available", "note": "Ant注册失败: 验证码页未出现密码入口"},
        {"id": 141, "email": "e@x.test", "status": "disabled", "note": "已有 OpenAI 账号"},
    ]

    def test_list_half_registered(self):
        import core.db as db
        with mock.patch.object(db, "_load_outlook", return_value=[dict(r) for r in self.ROWS]):
            out = auto_reg.list_half_registered()
        self.assertEqual([t["email"] for t in out], ["a@x.test", "b@x.test", "c@x.test"],
                         "只列 failed 且备注是半注册的；可用邮箱和已停用的都不算")
        self.assertEqual(out[0]["password"], "", "没记密码的留空，调用方改走一次性验证码登录")
        self.assertEqual(out[1]["password"], "Fk9!ppWw3/Qzr7", "备注里的密码要原样取出（含特殊字符）")
        self.assertEqual(out[2]["password"], "Xyz^1", "新格式的备注同样能取出密码")
        self.assertEqual(out[1]["pool_id"], 136)

    def _takeover(self, target, flow_mock=None, extra_args=None):
        """跑一遍 _takeover_one，外部依赖全部替身。flow_mock 可用来模拟登录失败。"""
        import core.account_export as account_export
        import core.email_provider as email_provider
        import core.name_samples as name_samples
        from config import codex as codex_cfg
        info = {"access_token": "tok", "user_id": "user-1", "user_name": "Ann", "expires": "E",
                "user": {"id": "user-1"}, "account": {"planType": "free"}, "cookies": [], "profile_id": "p1"}
        saved, released = [], []
        args = auto_reg._build_parser().parse_args(["takeover", "--all"])
        for key, value in (extra_args or {}).items():
            setattr(args, key, value)
        if flow_mock is None:
            flow_mock = mock.Mock(return_value=info)
        with contextlib.ExitStack() as ctx:
            ctx.enter_context(mock.patch.object(bridge, "ant_register_flow", flow_mock))
            ctx.enter_context(mock.patch.object(bridge, "pick_ant_proxy", lambda explicit=None: explicit or "http://u:p@192.0.2.58:6611"))
            ctx.enter_context(mock.patch.object(account_export, "save_account_data",
                                                side_effect=lambda **kw: saved.append(kw) or 4242))
            ctx.enter_context(mock.patch.object(email_provider, "release_email",
                                                side_effect=lambda email, status="available", note=None:
                                                released.append((email, status, note))))
            ctx.enter_context(mock.patch.object(email_provider, "resolve_email_source", lambda e: "outlook"))
            ctx.enter_context(mock.patch.object(name_samples, "random_display_name", return_value="Ann Lee"))
            ctx.enter_context(mock.patch.object(codex_cfg, "ENABLE_CODEX_AUTO", False))
            verify = ctx.enter_context(mock.patch.object(auto_reg, "_wait_twofa_and_verify",
                                                         side_effect=lambda res, a, **kw: res))
            res = auto_reg._takeover_one(dict(target), args)
        return res, flow_mock, saved, released, verify

    def test_takeover_with_known_password(self):
        res, flow, saved, released, verify = self._takeover({"email": "b@x.test", "password": "Fk9!ppWw3/Qzr7"})
        self.assertEqual(res["account_id"], 4242)
        self.assertEqual(res["mode"], "password")
        self.assertEqual(flow.call_args.kwargs["takeover"], "password")
        self.assertEqual(flow.call_args.kwargs["password"], "Fk9!ppWw3/Qzr7")
        self.assertEqual(flow.call_args.kwargs["proxy"], "http://u:p@192.0.2.58:6611")
        self.assertGreaterEqual(flow.call_args.kwargs["age"], 28, "接管时年龄取大一些，避开出生日期确认框")
        self.assertEqual(saved[0]["extra"]["registration_password"], "Fk9!ppWw3/Qzr7", "入库要带上密码")
        self.assertEqual(saved[0]["extra"]["codex"]["status"], "skipped")
        self.assertEqual(saved[0]["access_token"], "tok")
        self.assertEqual(released, [("b@x.test", "used", mock.ANY)])
        self.assertIn("账号 #4242", released[0][2])
        self.assertTrue(verify.call_args.kwargs["require_password"])
        self.assertNotIn("password", res, "结果里不能带密码")

    def test_explicit_proxy_is_passed_through(self):
        _res, flow, _saved, _released, _verify = self._takeover(
            {"email": "b@x.test", "password": "x"}, extra_args={"proxy": "http://u:p@192.0.2.99:1"})
        self.assertEqual(flow.call_args.kwargs["proxy"], "http://u:p@192.0.2.99:1")

    def test_takeover_without_password_uses_otp_login(self):
        res, flow, saved, released, verify = self._takeover({"email": "a@x.test", "password": ""})
        self.assertEqual(res["account_id"], 4242)
        self.assertEqual(res["mode"], "otp")
        self.assertEqual(flow.call_args.kwargs["takeover"], "otp")
        self.assertEqual(flow.call_args.kwargs["password"], "")
        self.assertNotIn("registration_password", saved[0]["extra"], "不知道的密码不能瞎编一个写进库")
        self.assertEqual(saved[0]["extra"]["takeover"]["mode"], "otp")
        self.assertIn("一次性验证码", released[0][2])
        self.assertFalse(verify.call_args.kwargs["require_password"])

    def test_login_failure_is_reported_not_raised(self):
        flow_mock = mock.Mock(side_effect=bridge.TakeoverLoginFailed("密码未被接受"))
        res, _flow, saved, released, _verify = self._takeover({"email": "b@x.test", "password": "x"}, flow_mock)
        self.assertFalse(res["ok"])
        self.assertEqual(res["kind"], "TakeoverLoginFailed")
        self.assertEqual(res["stage"], "login")
        self.assertIn("密码未被接受", res["detail"])
        self.assertEqual((saved, released), ([], []), "登录失败不该入库，也不该动邮箱池状态")

    def test_wait_and_verify_variants(self):
        import core.db as db
        args = auto_reg._build_parser().parse_args(["takeover", "--all"])

        def run(account, require):
            with mock.patch.object(auto_reg, "wait_twofa", return_value={"state": "success"}), \
                    mock.patch.object(db, "get_account", return_value=account):
                return auto_reg._wait_twofa_and_verify({"email": "a@x.test", "account_id": 1}, args,
                                                        started=auto_reg.time.time(),
                                                        require_password=require)

        full = {"access_token": "tok", "totp_secret": "ABC", "extra_json": '{"registration_password": "p"}'}
        self.assertTrue(run(full, True)["ok"])
        no_pwd = {"access_token": "tok", "totp_secret": "ABC", "extra_json": "{}"}
        self.assertEqual(run(no_pwd, True)["kind"], "incomplete", "注册出来的号没密码算不完整")
        relaxed = run(no_pwd, False)
        self.assertTrue(relaxed["ok"])
        self.assertEqual(relaxed["kind"], "no_password", "接管时没密码记录：算成功，但明确标出来待补密码")
        no_totp = {"access_token": "tok", "totp_secret": "", "extra_json": '{"registration_password": "p"}'}
        self.assertEqual(run(no_totp, False)["kind"], "incomplete")

    def test_wait_and_verify_reports_twofa_failure(self):
        args = auto_reg._build_parser().parse_args(["takeover", "--all"])
        with mock.patch.object(auto_reg, "wait_twofa", return_value={"state": "failed", "error": "boom"}):
            res = auto_reg._wait_twofa_and_verify({"email": "a@x.test", "account_id": 1}, args,
                                                  started=auto_reg.time.time())
        self.assertFalse(res["ok"])
        self.assertEqual(res["kind"], "twofa_failed")
        self.assertIn("boom", res["detail"])

    def test_run_takeover_only_touches_listed_emails(self):
        """指定的邮箱若不在半注册清单里（可能是正常可用邮箱）必须跳过，绝不拿去登录。"""
        listed = [{"email": "b@x.test", "pool_id": 136, "password": "pw", "note": "半注册"}]
        args = auto_reg._build_parser().parse_args(
            ["takeover", "--email", "B@x.test", "--email", "fresh@x.test"])
        with mock.patch.object(auto_reg, "list_half_registered", return_value=[dict(t) for t in listed]), \
                mock.patch.object(auto_reg, "collect_preflight",
                                  side_effect=AssertionError("--list 时不该做预检更不该执行")), \
                mock.patch.object(auto_reg, "_takeover_one",
                                  side_effect=AssertionError("--list 时不该执行接管")):
            code = auto_reg.run_takeover(argparse.Namespace(**{**vars(args), "list": True}))
        self.assertEqual(code, auto_reg.EXIT_OK)
        args.password = ""
        with mock.patch.object(auto_reg, "list_half_registered", return_value=[dict(t) for t in listed]), \
                mock.patch.object(auto_reg, "collect_preflight", return_value=[{"name": "n", "ok": True, "detail": ""}]), \
                mock.patch.object(auto_reg, "wait_until_idle", return_value=True), \
                mock.patch.object(auto_reg, "_takeover_one",
                                  side_effect=lambda target, a: {"ok": True, "email": target["email"], "account_id": 1,
                                                                 "proxy_exit": "x", "kind": "", "detail": "d"}) as one:
            code = auto_reg.run_takeover(args)
        self.assertEqual(code, auto_reg.EXIT_OK)
        self.assertEqual(one.call_count, 1, "只接管清单内的那一个")
        self.assertEqual(one.call_args.args[0]["email"].lower(), "b@x.test")

    def test_cli_needs_an_explicit_target(self):
        self.assertEqual(auto_reg.main(["takeover"]), auto_reg.EXIT_PREFLIGHT, "不带 --all/--email/--list 不能直接开跑")


class LoginHarness:
    """ant_node_bridge._ant_login_once 的替身环境（2FA 绑定、补密码都靠它登录）。

    用法:
        with LoginHarness(password_in_db="pw") as h:
            h.on_submit = ...      # 每次提交后改页面：url / 哪个输入框可见 / 是否已有登录 cookie
            h.on_pw_link = ...     # 点「使用密码继续」之后
            result, exc = h.run()
    h.last_typed 是最近一次敲入的内容：邮箱 / 密码 / 验证码。h.login() 直接置登录态。
    """

    EMAIL = "e@x.test"
    PW = "Pw12345678"
    CODE = "123456"
    VERIFY = "https://auth.openai.com/email-verification?auth_page=L"
    CONFIRM = "https://auth.openai.com/email-verification?auth_page=C"
    LOGIN_PWD = "https://auth.openai.com/log-in/password"
    MFA = "https://auth.openai.com/mfa-challenge/abc123?auth_page=x"
    HOME = "https://chatgpt.com/"
    OTHER_AUTH_PAGE = "https://auth.openai.com/authorize/resume/xyz"   # 不在 chatgpt.com：只有 cookie 判定能成功
    COOKIE = [{"name": "__Secure-next-auth.session-token", "value": "t", "domain": "chatgpt.com"}]

    def __init__(self, password_in_db=PW, otp_code=CODE):
        self.page = FakePage()
        self.password_in_db = password_in_db
        self.otp_code = otp_code
        self.clock = [1000.0]
        self.submits = 0
        self.typed = []
        self.otp_waiter_calls = []
        self.pw_link_clicks = 0
        self.cookies = []
        self.on_submit = lambda h: None
        self.on_pw_link = lambda h: "a:使用密码继续"
        self.on_mfa = lambda h: False             # 进入 MFA（动态码）页时做什么；返回是否已处理
        # real_mfa=True 时不替换 _handle_mfa_if_present，走真实的动态码逻辑（配 totp_codes 用）
        self.real_mfa = False
        self.totp_codes = iter(())                # 依次返回的动态码

    @property
    def last_typed(self):
        return self.typed[-1] if self.typed else ""

    def time(self):
        self.clock[0] += 1.0
        return self.clock[0]

    def login(self, url=HOME):
        """置登录态：有 session cookie，页面停到 url。"""
        self.cookies = list(self.COOKIE)
        self.page.url = url

    # ---- 常用编排 ----
    def show_verification_page(self, *, otp=True):
        """提交邮箱后的验证码页：验证码框和「使用密码继续」入口同页。"""
        self.page.url = self.VERIFY
        self.page.otp_visible = otp

    def show_password_page(self):
        self.page.url = self.LOGIN_PWD
        self.page.otp_visible = False
        self.page.pwd_visible = True

    def on_pw_link_default(self, h):
        self.show_password_page()
        return "a:使用密码继续"

    def _submit(self, pg, specs, *a, **kw):
        self.submits += 1
        self.on_submit(self)
        return True        # 真实 _human_submit 点到了就返回 True；返回假值流程会去按回车

    def _type(self, pg, css, text, **kw):
        self.typed.append(text)
        return True

    def _click(self, pg, patterns, *a, **kw):
        self.pw_link_clicks += 1
        return self.on_pw_link(self)

    def _otp_waiter(self, after_ts):
        self.otp_waiter_calls.append(after_ts)
        return self.otp_code

    def __enter__(self):
        import core.db as db
        page = self.page
        self._ctx = ctx = contextlib.ExitStack()

        @contextlib.contextmanager
        def fake_ant_page(proxy_url, email=None):
            yield page, object(), mock.Mock(profile_id="p1")

        ctx.enter_context(mock.patch.object(nb, "_ant_page", fake_ant_page))
        ctx.enter_context(mock.patch.object(nb, "_human_type", self._type))
        ctx.enter_context(mock.patch.object(nb, "_human_submit", self._submit))
        ctx.enter_context(mock.patch.object(nb, "_force_click_by_text", self._click))
        for name in ("_click_entry_button", "_select_workspace_if_present"):
            ctx.enter_context(mock.patch.object(nb, name, lambda *a, **kw: False))
        if self.real_mfa:
            # 流程测试只关心「被拒后换码重试」：每次取码从 totp_codes 里取下一个。
            # 取码时机（窗口、不重复用被拒的码）由 TotpChallengeTest 单独测 _fresh_totp。
            ctx.enter_context(mock.patch.object(nb, "_fresh_totp",
                                                lambda email, previous="": next(self.totp_codes, "")))
        else:
            ctx.enter_context(mock.patch.object(nb, "_handle_mfa_if_present",
                                               lambda pg, em, **kw: self.on_mfa(self)))
        ctx.enter_context(mock.patch.object(nb, "_detect_challenge", lambda pg: {"active": False}))
        for name in ("_wait_out_challenge", "_heal_chrome_error"):
            ctx.enter_context(mock.patch.object(nb, name, lambda *a, **kw: None))
        ctx.enter_context(mock.patch.object(nb, "_export_cookies", lambda c: list(self.cookies)))
        ctx.enter_context(mock.patch.object(db, "get_account_by_email", lambda e: {"id": 1}))
        ctx.enter_context(mock.patch.object(db, "_extract_registration_password",
                                            lambda row: self.password_in_db if row else ""))
        ctx.enter_context(mock.patch("time.time", self.time))
        ctx.enter_context(mock.patch("time.sleep", lambda s: None))
        return self

    def __exit__(self, *exc):
        self._ctx.__exit__(*exc)
        return False

    def run(self):
        """返回 (结果, 异常)：成功时结果是被 (_ant_login_once) 返回的 (cookies, info)。"""
        result, exc = None, None
        try:
            result = nb._ant_login_once(self.EMAIL, "http://u:p@192.0.2.10:1", self._otp_waiter, "outlook", 1)
        except Exception as e:  # noqa: BLE001
            exc = e
        return result, exc


class NodeLoginFlowTest(unittest.TestCase):
    """节点登录流程（2FA 绑定 / 补密码共用）。两条回归来自 2026-11-24 接管账号 #185、#186 时的 2FA 失败。"""

    def test_password_login_is_detected_by_session_cookie(self):
        """回归：密码路径以前 otp_round 未定义，按 cookie 判成功时会 NameError 并被兜底吞掉，
        全靠后面的 session 探针顶着；页面不在 chatgpt.com 时就会误判成登录失败。"""
        with LoginHarness() as h:
            def on_submit(h_):
                if h_.submits == 1:
                    h_.show_verification_page()
                else:                       # 密码提交 → 登录成功，但页面还停在 auth.openai.com
                    h_.login(h_.OTHER_AUTH_PAGE)
            h.on_submit, h.on_pw_link = on_submit, h.on_pw_link_default
            result, exc = h.run()
        self.assertIsNone(exc)
        cookies, info = result
        self.assertEqual(cookies, LoginHarness.COOKIE)
        self.assertEqual(info["login"], "browser_pwd", "标记要如实反映走的是密码登录")
        self.assertEqual(h.typed, [LoginHarness.EMAIL, LoginHarness.PW])
        self.assertEqual((h.otp_waiter_calls, h.pw_link_clicks), ([], 1))

    def test_account_without_known_password_logs_in_by_otp(self):
        """回归（#186）：库里没有密码时不能点「使用密码继续」——账号在 OpenAI 侧其实有密码，
        点进去只会落到密码页然后卡死。验证码登录对这类账号同样可用。"""
        with LoginHarness(password_in_db="") as h:
            def on_submit(h_):
                if h_.submits == 1:
                    h_.show_verification_page()      # 页面上有密码入口，也有验证码框
                else:
                    h_.login()
            h.on_submit = on_submit
            result, exc = h.run()
        self.assertIsNone(exc, "无密码账号应能用验证码登录：%r" % (exc,))
        self.assertEqual(result[1]["login"], "browser_otp")
        self.assertEqual(h.pw_link_clicks, 0, "没有密码就别去点密码入口")
        self.assertEqual(h.typed, [LoginHarness.EMAIL, LoginHarness.CODE])
        self.assertEqual(len(h.otp_waiter_calls), 1)

    def test_email_verification_after_password_is_handled(self):
        """回归（#185）：密码登录通过后 OpenAI 又要验一次邮箱，要取码敲入，而不是干等到超时。"""
        with LoginHarness() as h:
            def on_submit(h_):
                if h_.submits == 1:
                    h_.show_verification_page()
                elif h_.last_typed == LoginHarness.PW:      # 密码提交 → 再验一次邮箱
                    h_.page.url = h_.CONFIRM
                    h_.page.pwd_visible = False
                    h_.page.otp_visible = True
                else:                                       # 验证码提交 → 登录成功
                    h_.login()
            h.on_submit, h.on_pw_link = on_submit, h.on_pw_link_default
            result, exc = h.run()
        self.assertIsNone(exc, "密码后的邮箱验证应被处理：%r" % (exc,))
        self.assertEqual(result[1]["login"], "browser_pwd")
        self.assertEqual(h.typed, [LoginHarness.EMAIL, LoginHarness.PW, LoginHarness.CODE])
        self.assertEqual(len(h.otp_waiter_calls), 1)
        self.assertLess(h.clock[0] - 1000.0, 120, "应及时完成，不是熬到登录超时")

    def test_missing_otp_after_password_fails_fast(self):
        with LoginHarness(otp_code="") as h:
            def on_submit(h_):
                if h_.submits == 1:
                    h_.show_verification_page()
                elif h_.last_typed == LoginHarness.PW:
                    h_.page.url = h_.CONFIRM
                    h_.page.pwd_visible = False
                    h_.page.otp_visible = True
            h.on_submit, h.on_pw_link = on_submit, h.on_pw_link_default
            _result, exc = h.run()
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("验证码未取到", str(exc))
        self.assertLess(h.clock[0] - 1000.0, 200, "取不到码要立刻失败，不能被兜底吞成干等")

    def test_direct_password_page_without_link(self):
        with LoginHarness() as h:
            def on_submit(h_):
                if h_.submits == 1:
                    h_.show_password_page()
                else:
                    h_.login(h_.OTHER_AUTH_PAGE)
            h.on_submit = on_submit
            result, exc = h.run()
        self.assertIsNone(exc)
        self.assertEqual((result[1]["login"], h.pw_link_clicks), ("browser_pwd", 0))

    def test_password_page_without_known_password_still_fails_clearly(self):
        """没有密码却直接落到密码页（没有验证码这条路）：明确报错，不乱试。"""
        with LoginHarness(password_in_db="") as h:
            h.on_submit = lambda h_: h_.show_password_page()
            _result, exc = h.run()
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("库里没有密码", str(exc))
        self.assertEqual(h.typed, [LoginHarness.EMAIL])

    def test_otp_path_still_prefers_password_link_when_available(self):
        """有密码的账号：验证码框先出现、密码入口渲染更慢，也要等到它并优先走密码（原行为不变）。"""
        with LoginHarness() as h:
            calls = {"n": 0}

            def on_pw_link(h_):
                calls["n"] += 1
                if calls["n"] == 1:
                    return ""                     # 第一次扑空：密码入口还没渲染出来
                h_.show_password_page()
                return "a:使用密码继续"

            def on_submit(h_):
                if h_.submits == 1:
                    h_.show_verification_page()
                else:
                    h_.login(h_.OTHER_AUTH_PAGE)

            h.on_submit, h.on_pw_link = on_submit, on_pw_link
            result, exc = h.run()
        self.assertIsNone(exc)
        self.assertEqual(result[1]["login"], "browser_pwd")
        self.assertEqual((h.otp_waiter_calls, h.pw_link_clicks), ([], 2))
        self.assertEqual(h.typed, [LoginHarness.EMAIL, LoginHarness.PW])

    def test_mfa_page_after_otp_login_is_not_taken_for_another_email_otp_round(self):
        """回归（2026-11-24 账号 #186 补密码）：验证码登录后进了 MFA 动态码页。那一页上也有数字输入框，
        不看地址就会把它当成「第 2 轮邮箱验证」，去等一封永远不会来的邮件，直到超时。
        应该交给后面的 MFA 处理。"""
        with LoginHarness(password_in_db="") as h:
            def on_submit(h_):
                if h_.submits == 1:
                    h_.show_verification_page()
                else:
                    h_.page.url = h_.MFA
                    h_.page.otp_visible = True        # MFA 页上的动态码输入框也是数字框

            def on_mfa(h_):
                h_.login()                            # 动态码验证通过
                return True

            h.on_submit, h.on_mfa = on_submit, on_mfa
            result, exc = h.run()
        self.assertIsNone(exc, "MFA 页不应被当成又一轮邮箱验证：%r" % (exc,))
        self.assertEqual(result[1]["login"], "browser_otp")
        self.assertEqual(h.typed, [LoginHarness.EMAIL, LoginHarness.CODE])
        self.assertEqual(len(h.otp_waiter_calls), 1, "不该为 MFA 页再取一轮邮箱验证码")
        self.assertLess(h.clock[0] - 1000.0, 120, "应及时进入 MFA 处理，而不是干等")

    def test_workspace_selection_after_otp_login_is_not_taken_for_another_otp_round(self):
        """工作空间选择页同理：不是邮箱验证。"""
        with LoginHarness(password_in_db="") as h:
            def on_submit(h_):
                if h_.submits == 1:
                    h_.show_verification_page()
                else:
                    h_.page.url = "https://auth.openai.com/workspace/select?auth_page=x"
                    h_.page.otp_visible = True

            def on_mfa(h_):
                h_.login()
                return True

            h.on_submit, h.on_mfa = on_submit, on_mfa
            result, exc = h.run()
        self.assertIsNone(exc)
        self.assertEqual(len(h.otp_waiter_calls), 1)

    def test_real_second_otp_round_is_still_fed(self):
        """验证完又要求再验一次邮箱（地址是 email-verification）：仍然要取新码、再喂一轮。"""
        with LoginHarness(password_in_db="") as h:
            def on_submit(h_):
                if h_.submits == 1:
                    h_.show_verification_page()
                elif h_.submits == 2:
                    h_.page.url = h_.CONFIRM             # 第 2 轮：又是邮箱验证页
                    h_.page.otp_visible = True
                else:
                    h_.login()

            h.on_submit = on_submit
            result, exc = h.run()
        self.assertIsNone(exc)
        self.assertEqual(h.typed, [LoginHarness.EMAIL, LoginHarness.CODE, LoginHarness.CODE])
        self.assertEqual(len(h.otp_waiter_calls), 2)


class TotpChallengeTest(unittest.TestCase):
    """MFA 页输入动态码（2026-11-24 账号 #186 补密码时实测：同一步骤一次失败一次成功）。

    原实现只敲一次、死等 60 秒，既不看码离过期还有多久，也不换码重试。
    """

    @staticmethod
    def _scenario(h, *, accept_on_attempt):
        """验证码登录后进入 MFA 页；动态码被拒时页面原地不动，第 accept_on_attempt 次才通过。"""
        seen = {"n": 0}

        def on_submit(h_):
            if h_.submits == 1:
                h_.show_verification_page()
            elif h_.last_typed == LoginHarness.CODE:          # 邮箱验证码 → MFA 页
                h_.page.url = h_.MFA
                h_.page.otp_visible = True
            else:                                             # 动态码
                seen["n"] += 1
                if seen["n"] >= accept_on_attempt:
                    h_.login()

        h.on_submit = on_submit

    def _run(self, codes, *, accept_on_attempt):
        """用真实的 _handle_mfa_if_present 走一遍登录。注意开关要在进入 with 之前设好：
        替身是在 __enter__ 里安装的。"""
        h = LoginHarness(password_in_db="")
        h.real_mfa = True
        h.totp_codes = iter(codes)
        self._scenario(h, accept_on_attempt=accept_on_attempt)
        with h:
            result, exc = h.run()
        return h, result, exc

    def test_accepted_first_time(self):
        h, result, exc = self._run(["111111", "222222"], accept_on_attempt=1)
        self.assertIsNone(exc, repr(exc))
        self.assertEqual(result[1]["login"], "browser_otp")
        self.assertEqual(h.typed, [LoginHarness.EMAIL, LoginHarness.CODE, "111111"])
        self.assertEqual(len(h.otp_waiter_calls), 1)

    def test_rejected_totp_is_retried_with_a_fresh_code(self):
        h, result, exc = self._run(["111111", "222222", "333333"], accept_on_attempt=3)
        self.assertIsNone(exc, "被拒的动态码应换新码重试，而不是死等后报错：%r" % (exc,))
        self.assertEqual(result[1]["login"], "browser_otp")
        self.assertEqual(h.typed[2:], ["111111", "222222", "333333"])
        self.assertEqual(len(h.otp_waiter_calls), 1, "重试动态码不该再取邮箱验证码")

    def test_gives_up_after_three_rejected_codes(self):
        h, _result, exc = self._run(["111111", "222222", "333333", "444444"], accept_on_attempt=99)
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("未离开质询页", str(exc))
        self.assertEqual(h.typed[2:], ["111111", "222222", "333333"], "最多试 3 次")

    def test_no_totp_secret_is_reported(self):
        _h, _result, exc = self._run([""], accept_on_attempt=1)
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("没有 totp_secret", str(exc))

    def test_fresh_totp_waits_for_a_new_window_when_needed(self):
        """取码时机：窗口剩余够多、且和上次被拒的码不同 → 立即用；
        窗口快结束了、或码和上次被拒的相同 → 等到下一个窗口再取（假时钟随 sleep 前进）。"""
        import core.codex_oauth as codex_oauth
        # (窗口已过秒数, 上次被拒的码, 依次可取的码, 是否应等待, 期望返回的码)
        for elapsed, previous, codes, expect_sleep, expected in (
            (10.0, "", ["555555"], False, "555555"),
            (25.0, "", ["555555", "777777"], True, "777777"),
            (10.0, "555555", ["555555", "666666"], True, "666666"),
        ):
            # 假时钟起点取 30 的整数倍，窗口内已过的秒数才等于 elapsed
            clock = [1020.0 + elapsed]
            sleeps = []

            def fake_time():
                clock[0] += 0.05
                return clock[0]

            def fake_sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds

            it = iter(codes)
            with mock.patch("time.time", fake_time), \
                    mock.patch("time.sleep", fake_sleep), \
                    mock.patch.object(codex_oauth, "_account_totp_code", lambda email: next(it)):
                code = nb._fresh_totp(LoginHarness.EMAIL, previous=previous)
            self.assertEqual(code, expected, (elapsed, previous))
            self.assertEqual(bool(sleeps), expect_sleep, (elapsed, previous, sleeps))


if __name__ == "__main__":
    unittest.main(verbosity=2)
