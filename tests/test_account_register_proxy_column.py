import unittest
from unittest.mock import patch

from webui import app as webui_app
from webui.app import _compact_account_for_list, _proxy_name_map


POOL = [
    "socks5h://user:p%40ss@1.2.3.4:1080#美国家宽 1",
    "# http://10.0.0.1:8080#已停用日本",
    "http://10.0.0.2:8080",
]


class RegisterProxyColumnTests(unittest.TestCase):
    def names(self):
        with patch.object(webui_app.config_editor, "get_field_value", return_value=list(POOL)):
            return _proxy_name_map()

    def compact(self, proxy_used):
        return _compact_account_for_list({"id": 1, "email": "a@b.test", "proxy_used": proxy_used}, self.names())

    def test_pool_names_cover_disabled_rows_and_skip_unnamed(self):
        self.assertEqual(
            self.names(),
            {"socks5h://user:p%40ss@1.2.3.4:1080": "美国家宽 1", "http://10.0.0.1:8080": "已停用日本"},
        )

    def test_named_proxy_resolves_including_shorthand_and_disabled(self):
        self.assertEqual(self.compact("socks5h://user:p%40ss@1.2.3.4:1080")["register_proxy_name"], "美国家宽 1")
        self.assertEqual(self.compact("http://10.0.0.1:8080")["register_proxy_name"], "已停用日本")
        self.assertEqual(self.compact("10.0.0.1:8080")["register_proxy_name"], "已停用日本")

    def test_environment_tag_shown_as_is(self):
        self.assertEqual(self.compact("skyvern:jp")["register_proxy_name"], "skyvern:jp")
        self.assertEqual(self.compact("browser_use:default")["register_proxy_name"], "browser_use:default")

    def test_never_leaks_address_or_credentials(self):
        for value in (
            "",
            None,
            "socks5h://secret:topsecret@9.9.9.9:1080",
            "http://10.0.0.2:8080",
            "9.9.9.9:1080:secret:topsecret",
            "secret:topsecret:9.9.9.9:1080",
        ):
            out = self.compact(value)
            self.assertNotIn("register_proxy_name", out, value)
            self.assertNotIn("proxy_used", out, value)
            self.assertNotIn("topsecret", repr(out), value)

    def test_unreadable_pool_degrades_to_empty_column(self):
        with patch.object(webui_app.config_editor, "get_field_value", side_effect=OSError(".env 不可读")):
            self.assertEqual(_proxy_name_map(), {})


if __name__ == "__main__":
    unittest.main()
