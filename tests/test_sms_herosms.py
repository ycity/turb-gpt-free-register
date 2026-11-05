# -*- coding: utf-8 -*-
"""HeroSMS 接码通道。

HeroSMS 官方声明兼容 SMS-Activate：action、参数、setStatus 的 3/6/8 都和
SMSBower 同族。但错误响应格式不同，实测（2026-11-05）：

    getNumber 失败   -> HTTP 200 + 纯文本 "BAD_KEY"
    getStatus 失败   -> HTTP 401 + JSON {"title":"BAD_KEY","details":"Unauthorized"}
    不存在的激活 ID  -> HTTP 404 + JSON {"title":"NOT_FOUND",...}
    缺 api_key       -> HTTP 422 + JSON {"title":"UNPROCESSABLE_ENTITY",...}

直接复用 _request_smsbower 的话，它「非 200 一律抛 HTTP 错误」，轮询验证码
会在 getStatus 这步必然失败，所以必须单独解析。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "core" / "sms_provider.py").read_text(encoding="utf-8")


class FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


class FakeHttp:
    def __init__(self, resp):
        self._resp = resp
        self.last_url = None
        self.last_params = None

    def get(self, url, params=None, **kw):
        self.last_url = url
        self.last_params = params
        return self._resp

    def close(self):
        pass


def _cfg_patch(**kw):
    """临时改 config.codex 的值。"""
    from config import codex
    old = {k: getattr(codex, k, None) for k in kw}

    class Ctx:
        def __enter__(self):
            for k, v in kw.items():
                setattr(codex, k, v)

        def __exit__(self, *a):
            for k, v in old.items():
                setattr(codex, k, v)
    return Ctx()


class BaseUrlTest(unittest.TestCase):
    def test_appends_path_when_only_domain_given(self):
        from core.sms_provider import _herosms_base
        with _cfg_patch(HEROSMS_API_BASE="https://hero-sms.com"):
            self.assertEqual(_herosms_base(),
                             "https://hero-sms.com/stubs/handler_api.php")

    def test_keeps_full_path(self):
        from core.sms_provider import _herosms_base
        full = "https://hero-sms.com/stubs/handler_api.php"
        with _cfg_patch(HEROSMS_API_BASE=full):
            self.assertEqual(_herosms_base(), full)

    def test_tolerates_trailing_slash(self):
        from core.sms_provider import _herosms_base
        with _cfg_patch(HEROSMS_API_BASE="https://hero-sms.com/"):
            self.assertEqual(_herosms_base(),
                             "https://hero-sms.com/stubs/handler_api.php")

    def test_empty_base_is_rejected(self):
        from core.sms_provider import _herosms_base, SmsProviderError
        with _cfg_patch(HEROSMS_API_BASE=""):
            with self.assertRaises(SmsProviderError):
                _herosms_base()


class ErrorParsingTest(unittest.TestCase):
    """错误格式的两副面孔都要认。"""

    def _req(self, text, status=200):
        from core.sms_provider import _request_herosms
        with _cfg_patch(HEROSMS_API_KEY="k",
                        HEROSMS_API_BASE="https://hero-sms.com"):
            return _request_herosms(FakeHttp(FakeResp(text, status)),
                                    {"action": "getStatus"})

    def test_plain_text_bad_key(self):
        """getNumber 这条路返回纯文本，HTTP 还是 200。"""
        from core.sms_provider import SmsProviderError
        with self.assertRaises(SmsProviderError) as cm:
            self._req("BAD_KEY", 200)
        self.assertIn("BAD_KEY", str(cm.exception))

    def test_json_bad_key_with_401(self):
        """这条是 SMSBower 解析器会漏掉的：401 + JSON。"""
        from core.sms_provider import SmsProviderError
        with self.assertRaises(SmsProviderError) as cm:
            self._req('{"title":"BAD_KEY","details":"Unauthorized"}', 401)
        self.assertIn("BAD_KEY", str(cm.exception))

    def test_json_not_found(self):
        from core.sms_provider import SmsProviderError
        with self.assertRaises(SmsProviderError) as cm:
            self._req('{"title":"NOT_FOUND","details":"Activation Not Found"}', 404)
        self.assertIn("NOT_FOUND", str(cm.exception))

    def test_unprocessable_entity(self):
        from core.sms_provider import SmsProviderError
        with self.assertRaises(SmsProviderError):
            self._req('{"title":"UNPROCESSABLE_ENTITY","details":"Validation failed"}', 422)

    def test_no_balance_maps_to_balance_error(self):
        from core.sms_provider import SmsNoBalanceError
        with self.assertRaises(SmsNoBalanceError):
            self._req("NO_BALANCE", 200)

    def test_no_numbers_maps_to_numbers_error(self):
        from core.sms_provider import SmsNoNumbersError
        with self.assertRaises(SmsNoNumbersError):
            self._req("NO_NUMBERS", 200)

    def test_bad_service_mentions_dr(self):
        """OpenAI 的服务代码是 dr（实测 getServicesList 确认），要在报错里说清楚。"""
        from core.sms_provider import SmsProviderError
        with self.assertRaises(SmsProviderError) as cm:
            self._req("BAD_SERVICE", 200)
        self.assertIn("dr", str(cm.exception))

    def test_success_passes_through(self):
        self.assertEqual(self._req("STATUS_WAIT_CODE", 200), "STATUS_WAIT_CODE")

    def test_balance_passes_through(self):
        self.assertEqual(self._req("ACCESS_BALANCE:3.9441", 200),
                         "ACCESS_BALANCE:3.9441")

    def test_missing_key_is_rejected_before_request(self):
        from core.sms_provider import _request_herosms, SmsProviderError
        with _cfg_patch(HEROSMS_API_KEY="",
                        HEROSMS_API_BASE="https://hero-sms.com"):
            with self.assertRaises(SmsProviderError):
                _request_herosms(FakeHttp(FakeResp("x")), {"action": "getBalance"})

    def test_sends_api_key_in_query(self):
        from core.sms_provider import _request_herosms
        http = FakeHttp(FakeResp("OK"))
        with _cfg_patch(HEROSMS_API_KEY="mykey",
                        HEROSMS_API_BASE="https://hero-sms.com"):
            _request_herosms(http, {"action": "getBalance"})
        self.assertEqual(http.last_params.get("api_key"), "mykey")


class WiringTest(unittest.TestCase):
    def test_acquire_number_has_branch(self):
        idx = SRC.index("def acquire_number")
        body = SRC[idx:idx + 2500]
        self.assertIn('_provider() == "herosms"', body)

    def test_openai_service_code_normalized_to_dr(self):
        idx = SRC.index("def acquire_number")
        body = SRC[idx:idx + 2500]
        self.assertIn('("openai", "chatgpt")', body)

    def test_wait_for_code_has_branch(self):
        idx = SRC.index("def wait_for_sms_code")
        body = SRC[idx:SRC.index("def set_status")]
        self.assertIn('provider == "herosms"', body)
        self.assertIn("STATUS_OK:", body)

    def test_set_status_skips_status_1(self):
        """和 SMSBower 一样没有 status=1，发过去会被判参数错误。"""
        idx = SRC.index("def set_status")
        body = SRC[idx:SRC.index("def complete")]
        self.assertIn('_provider() == "herosms"', body)

    def test_cancel_waits_for_min_activation_time(self):
        """HeroSMS 也有 2 分钟最短激活时间——实测提前取消会被
        HTTP 409 EARLY_CANCEL_DENIED 拒绝（minActivationTime=120），钱退不回来。
        所以要走 _do_cancel_sync 等够时间，不能像 SMSBower 那样立刻取消。"""
        idx = SRC.index("def cancel")
        body = SRC[idx:]
        hero = body[body.index('_provider() == "herosms"'):]
        hero = hero[:hero.index('_provider() == "smsbower"')]
        self.assertIn("_do_cancel_sync(", hero)
        self.assertIn("EARLY_CANCEL_DENIED", hero)


class ConfigTest(unittest.TestCase):
    def test_keys_exist(self):
        from config import codex
        self.assertTrue(hasattr(codex, "HEROSMS_API_BASE"))
        self.assertTrue(hasattr(codex, "HEROSMS_API_KEY"))

    def test_key_reads_from_env_not_hardcoded(self):
        """密钥不能明文写在 config，会进 git。"""
        cfg = (ROOT / "config" / "codex.py").read_text(encoding="utf-8")
        idx = cfg.index("HEROSMS_API_KEY")
        line = cfg[idx:cfg.index("\n", idx)]
        self.assertIn("env_str", line)

    def test_env_override_declared(self):
        cfg = (ROOT / "config" / "codex.py").read_text(encoding="utf-8")
        self.assertIn("'HEROSMS_API_KEY': 'str'", cfg)
        self.assertIn("'HEROSMS_API_BASE': 'str'", cfg)

    def test_provider_help_lists_herosms(self):
        ed = (ROOT / "webui" / "config_editor.py").read_text(encoding="utf-8")
        idx = ed.index('"key": "SMS_PROVIDER"')
        self.assertIn("herosms", ed[idx:idx + 400])


class UiTabTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "webui" / "templates" / "index.html").read_text(
            encoding="utf-8")

    def test_tab_registered_in_order(self):
        self.assertIn("'SMSBower', 'HeroSMS', 'H 接码'", self.html)

    def test_keys_routed_to_tab(self):
        self.assertIn("key.startsWith('HEROSMS_')", self.html)

    def test_editor_fields_defined(self):
        ed = (ROOT / "webui" / "config_editor.py").read_text(encoding="utf-8")
        self.assertIn('"key": "HEROSMS_API_BASE"', ed)
        self.assertIn('"key": "HEROSMS_API_KEY"', ed)

    def test_api_key_marked_secret(self):
        ed = (ROOT / "webui" / "config_editor.py").read_text(encoding="utf-8")
        idx = ed.index('"key": "HEROSMS_API_KEY"')
        block = ed[idx:idx + 400]
        self.assertIn('"secret": True', block)
        self.assertIn('"storage": "env"', block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
