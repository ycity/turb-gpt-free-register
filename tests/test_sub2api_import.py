# -*- coding: utf-8 -*-
"""跑通 Codex 的账号一键导入 sub2api。

sub2api 只收 oauth / apikey，不收 agent_identity（实测 400
「'Type' failed on the 'oneof' tag」）。所以导入的是 Codex 授权产物里的
access_token / refresh_token，拼成 oauth 凭据，和它现有的 AG*-Team1 一致。

命名：AG + 订阅编号 + -Team1，周限额编号前加 Z。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "core" / "sub2api_import.py").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


class NamingTest(unittest.TestCase):
    def test_five_hour_format(self):
        from core.sub2api_import import build_account_name
        self.assertEqual(build_account_name(49, "5h"), "AG49-Team1")
        self.assertEqual(build_account_name(76, "5h"), "AG76-Team1")

    def test_weekly_gets_z_prefix(self):
        from core.sub2api_import import build_account_name
        self.assertEqual(build_account_name(8, "weekly"), "AGZ8-Team1")
        self.assertEqual(build_account_name(9, "weekly"), "AGZ9-Team1")

    def test_missing_number_rejected(self):
        """没编号就拼不出名字，不能随便编一个。"""
        from core.sub2api_import import Sub2apiError, build_account_name
        for bad in (None, 0, ""):
            with self.assertRaises(Sub2apiError):
                build_account_name(bad, "5h")

    def test_unknown_kind_treated_as_five_hour(self):
        from core.sub2api_import import build_account_name
        self.assertEqual(build_account_name(50, ""), "AG50-Team1")


class CredentialsTest(unittest.TestCase):
    """身份信息藏在 id_token 的 OpenAI 命名空间里，顶层字段不够用。"""

    @staticmethod
    def _id_token(auth=None, email="a@b.com", aud=None):
        import base64
        import json as _json

        def seg(d):
            raw = _json.dumps(d).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        payload = {
            "email": email,
            "aud": aud if aud is not None else ["app_TESTCLIENT"],
            "https://api.openai.com/auth": auth if auth is not None else {
                "chatgpt_account_id": "acc-123",
                "chatgpt_user_id": "user-456",
                "chatgpt_plan_type": "team",
                "chatgpt_subscription_active_until": "2026-12-02T09:10:34+00:00",
                "organizations": [{"id": "org-789"}],
            },
        }
        return seg({"alg": "none"}) + "." + seg(payload) + ".sig"

    def _payload(self, **over):
        base = {
            "access_token": "at-" + "x" * 40,
            "refresh_token": "rt-" + "y" * 40,
            "id_token": self._id_token(),
            "account_id": "acc-fallback",
            "email": "top@level.com",
            "expired": "2026-11-15T10:08:32Z",
        }
        base.update(over)
        return base

    def test_maps_all_required_fields(self):
        from core.sub2api_import import build_credentials
        c = build_credentials(self._payload())
        self.assertEqual(c["client_id"], "app_TESTCLIENT")
        self.assertEqual(c["chatgpt_user_id"], "user-456")
        self.assertEqual(c["chatgpt_account_id"], "acc-123")
        self.assertEqual(c["organization_id"], "org-789")
        self.assertEqual(c["plan_type"], "team")
        self.assertTrue(c["access_token"])
        self.assertTrue(c["refresh_token"])

    def test_aud_list_takes_first(self):
        """aud 是数组，直接塞进去 sub2api 会拿到 ['app_x'] 这种字符串。"""
        from core.sub2api_import import build_credentials
        c = build_credentials(self._payload(
            id_token=self._id_token(aud=["app_FIRST", "app_SECOND"])))
        self.assertEqual(c["client_id"], "app_FIRST")

    def test_aud_string_also_works(self):
        from core.sub2api_import import build_credentials
        c = build_credentials(self._payload(
            id_token=self._id_token(aud="app_PLAIN")))
        self.assertEqual(c["client_id"], "app_PLAIN")

    def test_email_prefers_id_token(self):
        from core.sub2api_import import build_credentials
        c = build_credentials(self._payload())
        self.assertEqual(c["email"], "a@b.com")

    def test_account_id_falls_back_to_top_level(self):
        from core.sub2api_import import build_credentials
        c = build_credentials(self._payload(
            id_token=self._id_token(auth={"chatgpt_user_id": "u"})))
        self.assertEqual(c["chatgpt_account_id"], "acc-fallback")

    def test_no_organizations_is_tolerated(self):
        from core.sub2api_import import build_credentials
        c = build_credentials(self._payload(
            id_token=self._id_token(auth={"organizations": []})))
        self.assertEqual(c["organization_id"], "")

    def test_missing_tokens_rejected(self):
        from core.sub2api_import import Sub2apiError, build_credentials
        with self.assertRaises(Sub2apiError):
            build_credentials(self._payload(access_token=""))
        with self.assertRaises(Sub2apiError):
            build_credentials(self._payload(refresh_token=""))

    def test_missing_id_token_rejected(self):
        """没有 id_token 就拿不到 chatgpt_account_id，建出来的账号是废的。"""
        from core.sub2api_import import Sub2apiError, build_credentials
        with self.assertRaises(Sub2apiError):
            build_credentials(self._payload(id_token=""))


class ImportLogicTest(unittest.TestCase):
    def test_uses_oauth_not_agent_identity(self):
        """sub2api 实测拒绝 agent_identity，只能走 oauth。"""
        self.assertIn('"type": "oauth"', SRC)
        self.assertNotIn('"type": "agent_identity"', SRC)

    def test_reason_documented(self):
        self.assertIn("agent_identity", SRC[:900])

    def test_updates_existing_instead_of_duplicating(self):
        """重复建会让 sub2api 调度到失效凭据。"""
        idx = SRC.index("def import_account")
        body = SRC[idx:]
        self.assertIn("find_account_by_name", body)
        self.assertIn("updated", body)

    def test_groups_and_proxy_set_by_separate_put(self):
        """创建接口收下 group_ids 但不生效，实测必须单独 PUT。"""
        idx = SRC.index("def import_account")
        body = SRC[idx:]
        self.assertIn('patch = {"group_ids": groups}', body)
        self.assertIn("创建接口收下 group_ids 但不会生效", body)

    def test_proxy_matched_by_name(self):
        idx = SRC.index("def find_proxy_id")
        body = SRC[idx:idx + 700]
        self.assertIn(".lower()", body)

    def test_missing_proxy_does_not_block(self):
        """sub2api 里没有同名代理时要能继续导入，只是没代理。"""
        idx = SRC.index("def import_account")
        body = SRC[idx:]
        self.assertIn("logger.warning", body)


class ApiWiringTest(unittest.TestCase):
    def test_endpoint_registered(self):
        self.assertIn('@app.post("/api/accounts/<int:acc_id>/import-sub2")', APP)

    def test_requires_subscription_number(self):
        idx = APP.index("def api_account_import_sub2")
        body = APP[idx:idx + 2200]
        self.assertIn("subscription_no", body)
        self.assertIn("无法生成 sub2api 名称", body)

    def test_requires_codex_data(self):
        idx = APP.index("def api_account_import_sub2")
        body = APP[idx:idx + 2200]
        self.assertIn("get_codex_credential_payload", body)

    def test_resolves_proxy_name(self):
        idx = APP.index("def api_account_import_sub2")
        body = APP[idx:idx + 2200]
        self.assertIn("name_of_proxy", body)

    def test_helper_functions_exist(self):
        from core.db import get_codex_credential_payload  # noqa: F401
        from webui.proxy_pool import name_of_proxy  # noqa: F401


class ProxyLookupTest(unittest.TestCase):
    def test_name_of_proxy_is_reverse_of_resolve(self):
        from webui.proxy_pool import name_of_proxy, resolve_by_name
        url = resolve_by_name("Aaitr-Frontier")
        if not url:
            self.skipTest("代理池里没有 Aaitr-Frontier")
        self.assertEqual(name_of_proxy(url), "Aaitr-Frontier")

    def test_unknown_proxy_returns_empty(self):
        from webui.proxy_pool import name_of_proxy
        self.assertEqual(name_of_proxy("http://1.2.3.4:9999"), "")
        self.assertEqual(name_of_proxy(""), "")


class UiTest(unittest.TestCase):
    def test_button_rendered_on_success(self):
        self.assertIn('data-import-sub2="${esc(r.id)}"', HTML)

    def test_disabled_without_number(self):
        self.assertIn('<button disabled title="该账号还没有订阅编号">', HTML)

    def test_event_bound(self):
        self.assertIn("e.target.closest('[data-import-sub2]')", HTML)

    def test_handler_exists(self):
        self.assertIn("async function importOneAccountToSub2", HTML)

    def test_name_preview_matches_backend_rule(self):
        self.assertIn("subscription_kind === 'weekly' ? 'Z' : ''", HTML)


class ConfigTest(unittest.TestCase):
    def test_settings_exist(self):
        from config import sub2api as c
        for key in ("SUB2API_ADMIN_EMAIL", "SUB2API_ADMIN_PASSWORD",
                    "SUB2API_GROUP_IDS", "SUB2API_CONCURRENCY"):
            self.assertTrue(hasattr(c, key), key)

    def test_default_group_is_composite(self):
        from config import sub2api as c
        self.assertEqual(list(c.SUB2API_GROUP_IDS), [6])

    def test_password_overridable_by_env(self):
        cfg = (ROOT / "config" / "sub2api.py").read_text(encoding="utf-8")
        self.assertIn("'SUB2API_ADMIN_PASSWORD': 'str'", cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
