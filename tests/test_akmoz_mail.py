# -*- coding: utf-8 -*-
"""akmoz（闪取）邮箱源：素材导入 + 取码。

需求（2026-11-18）：用户会持续拿到这种格式的邮箱素材，
希望粘贴整段文本就能导入，后续注册/2FA/查活自动用它取码。

素材行：
  邮箱---邮箱密码---接码链接（直接验证码登录openai）：a@x.com----pwd----https://email.akmoz.com/email_query?email=a%40x.com

关键坑（实测）：
  email_query 那个链接返回的是 5 万字节的 HTML 前端页面，直接解析取不到码。
  真实接口是 POST /api/email/fetch_mails {"email": ...}，从页面 JS 里挖出来的。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

from core import akmoz_mail_client as ak  # noqa: E402

_SAMPLE = (
    "邮箱---邮箱密码---接码链接（直接验证码登录openai）："
    "MedanichKaniecki24@hotmail.com----estcpur96----"
    "https://email.akmoz.com/email_query?email=MedanichKaniecki24%40hotmail.com"
)


class ParseMaterialTest(unittest.TestCase):
    """素材解析——用户会直接粘贴整段文本。"""

    def test_parses_full_line_with_prefix(self):
        got = ak.parse_material(_SAMPLE)
        self.assertEqual(len(got), 1)
        r = got[0]
        self.assertEqual(r["email"], "MedanichKaniecki24@hotmail.com")
        self.assertEqual(r["password"], "estcpur96")
        self.assertIn("email.akmoz.com", r["code_url"])

    def test_parses_two_lines(self):
        text = _SAMPLE + "\n" + (
            "邮箱---邮箱密码---接码链接（直接验证码登录openai）："
            "MeczyworSiers37@hotmail.com----nzshkuw6643----"
            "https://email.akmoz.com/email_query?email=MeczyworSiers37%40hotmail.com")
        got = ak.parse_material(text)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1]["password"], "nzshkuw6643")

    def test_parses_without_prefix(self):
        got = ak.parse_material(
            "a@x.com----pwd----https://email.akmoz.com/email_query?email=a%40x.com")
        self.assertEqual(got[0]["email"], "a@x.com")
        self.assertEqual(got[0]["password"], "pwd")

    def test_email_only_gets_default_url(self):
        """只给邮箱时自动拼取码链接，别让用户手工拼。"""
        got = ak.parse_material("a@x.com")
        self.assertEqual(len(got), 1)
        self.assertIn("email.akmoz.com", got[0]["code_url"])
        self.assertIn("a%40x.com", got[0]["code_url"])

    def test_email_and_password_only(self):
        got = ak.parse_material("a@x.com----pwd")
        self.assertEqual(got[0]["password"], "pwd")
        self.assertIn("email.akmoz.com", got[0]["code_url"])

    def test_dedupes(self):
        """同一段文本里重复的邮箱只留一条。"""
        got = ak.parse_material(_SAMPLE + "\n" + _SAMPLE)
        self.assertEqual(len(got), 1)

    def test_skips_junk_lines(self):
        got = ak.parse_material("# 注释\n\n没有邮箱的一行\n" + _SAMPLE)
        self.assertEqual(len(got), 1)

    def test_accepts_equals_separator(self):
        got = ak.parse_material("a@x.com====pwd")
        self.assertEqual(got[0]["password"], "pwd")

    def test_english_colon_prefix(self):
        got = ak.parse_material("mail: a@x.com----pwd")
        self.assertEqual(got[0]["email"], "a@x.com")

    def test_password_not_mistaken_for_url(self):
        """密码位和链接位要分清，别把链接当密码存。"""
        got = ak.parse_material(_SAMPLE)
        self.assertNotIn("http", got[0]["password"])


class FetchOtpTest(unittest.TestCase):
    """取码：只认 after_ts 之后的 OpenAI 邮件。"""

    def _inbox(self, msgs):
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"channel": "REST V2", "inbox": msgs}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        return resp

    def _msg(self, code="123456", when="2026-11-18T10:00:00",
             frm="noreply@tm.openai.com"):
        return {
            "from": {"emailAddress": {"address": frm}},
            "subject": "Your ChatGPT code",
            "fullBody": "Your code is %s" % code,
            "receivedDateTime": when,
        }

    def test_extracts_openai_code(self):
        with patch("urllib.request.urlopen",
                   return_value=self._inbox([self._msg("654321")])), \
             patch.object(ak, "get_account_context", return_value=None):
            got = ak.fetch_latest_otp("a@x.com", after_ts=0, max_wait=5)
        self.assertEqual(got, "654321")

    def test_ignores_non_openai_mail(self):
        """Microsoft 的安全通知不能被当成验证码。"""
        junk = self._msg("999888",
                         frm="account-security-noreply@accountprotection.microsoft.com")
        junk["subject"] = "Microsoft 帐户安全信息已删除"
        junk["fullBody"] = "安全代码 999888"
        with patch("urllib.request.urlopen", return_value=self._inbox([junk])), \
             patch.object(ak, "get_account_context", return_value=None), \
             patch("time.sleep"):
            with self.assertRaises(ak.AkmozMailError):
                ak.fetch_latest_otp("a@x.com", after_ts=0, max_wait=1)

    def test_prefers_newest(self):
        old = self._msg("111111", when="2026-11-18T09:00:00")
        new = self._msg("222222", when="2026-11-18T10:00:00")
        with patch("urllib.request.urlopen",
                   return_value=self._inbox([old, new])), \
             patch.object(ak, "get_account_context", return_value=None):
            got = ak.fetch_latest_otp("a@x.com", after_ts=0, max_wait=5)
        self.assertEqual(got, "222222", "应该取最新那封")

    def test_timeout_raises(self):
        with patch("urllib.request.urlopen", return_value=self._inbox([])), \
             patch("time.sleep"):
            with self.assertRaises(ak.AkmozMailError):
                ak.fetch_latest_otp("a@x.com", after_ts=0, max_wait=1)

    def test_uses_post_api_not_html_page(self):
        """必须打 /api/email/fetch_mails，不能去解析 email_query 那个 HTML 页。"""
        import inspect
        self.assertEqual(ak._FETCH_PATH, "/api/email/fetch_mails")
        src = inspect.getsource(ak.fetch_inbox)
        self.assertIn("_FETCH_PATH", src)
        self.assertIn('method="POST"', src)
        self.assertNotIn("email_query\"", src.replace(
            'api_host_of(code_url) + "/email_query"', ""),
            "不该把 email_query 页面当数据源")

    def test_host_follows_code_url(self):
        """换域名部署时，以素材链接里的域名为准。"""
        self.assertEqual(
            ak.api_host_of("https://mail2.example.com/email_query?email=a"),
            "https://mail2.example.com")

    def test_blank_code_url_uses_default(self):
        self.assertIn("akmoz", ak.api_host_of(""))


class ProviderWiringTest(unittest.TestCase):
    """provider 层有 5 个分发点，漏一个就是半截功能。"""

    def setUp(self):
        self.src = (ROOT / "core" / "email_provider.py").read_text(
            encoding="utf-8")

    def test_registered_as_valid_source(self):
        from core.email_provider import _VALID_SOURCES
        self.assertIn("akmoz", _VALID_SOURCES)

    def test_parse_accepts_akmoz(self):
        from core.email_provider import parse_email_sources
        self.assertEqual(parse_email_sources("akmoz"), ["akmoz"])

    def test_pick_wired(self):
        self.assertIn("from core.akmoz_mail_client import pick_account",
                      self.src)

    def test_otp_wired(self):
        self.assertIn("from core.akmoz_mail_client import fetch_latest_otp",
                      self.src)

    def test_resolve_source_wired(self):
        """按邮箱反查来源——查活时靠它决定去哪取码。"""
        self.assertIn("db.get_akmoz_email_by_email", self.src)

    def test_resolve_checks_akmoz_before_outlook(self):
        """akmoz 邮箱也是 hotmail 域名，必须先于 outlook 判定，
        否则会被当成 outlook 去取码（#127 就栽在这）。"""
        a = self.src.index("db.get_akmoz_email_by_email")
        b = self.src.index("db.get_outlook_by_email")
        self.assertLess(a, b)


class DbPoolTest(unittest.TestCase):
    def test_source_registered(self):
        from core import db
        self.assertIn("akmoz", db._TABLES)
        self.assertEqual(db._EMAIL_SOURCES.get("akmoz"), "akmoz")

    def test_import_functions_exist(self):
        from core import db
        for name in ("import_akmoz_emails", "claim_next_akmoz_email",
                     "release_akmoz_email", "get_akmoz_email_by_email",
                     "akmoz_email_pool_summary", "list_akmoz_email_pool"):
            self.assertTrue(hasattr(db, name), "缺 %s" % name)

    def test_copy_line_round_trips(self):
        """复制出去的行要能原样粘回来再导入。"""
        from core import db
        line = db._akmoz_email_line({
            "email": "a@x.com", "password": "pwd",
            "code_url": "https://email.akmoz.com/email_query?email=a%40x.com"})
        got = ak.parse_material(line)
        self.assertEqual(got[0]["email"], "a@x.com")
        self.assertEqual(got[0]["password"], "pwd")

    def test_import_skips_duplicates(self):
        from core import db
        rows = [{"id": 1, "email": "a@x.com", "status": "used"}]
        with patch.object(db, "_load_akmoz_emails", return_value=rows), \
             patch.object(db, "_save_akmoz_emails"):
            ins, skip = db.import_akmoz_emails([{"email": "a@x.com"}])
        self.assertEqual((ins, skip), (0, 1), "重复导入会把 used 洗成 available")

    def test_import_rejects_non_email(self):
        from core import db
        with patch.object(db, "_load_akmoz_emails", return_value=[]), \
             patch.object(db, "_save_akmoz_emails"):
            ins, skip = db.import_akmoz_emails([{"email": "不是邮箱"}])
        self.assertEqual(ins, 0)

    def test_pool_page_knows_akmoz(self):
        """list_email_pool_page 里有个硬编码白名单，不加就静默回落成
        outlook——表现是"筛 akmoz 却返回一堆 outlook 邮箱"，很难看出来。"""
        import inspect
        from core import db
        src = inspect.getsource(db.list_email_pool_page)
        i = src.index("if source not in")
        self.assertIn("akmoz", src[i:i + 200])

    def test_delete_knows_akmoz(self):
        """删除路径的白名单不加会直接抛 ValueError。"""
        import inspect
        from core import db
        src = inspect.getsource(db.delete_email_pool)
        i = src.index("if source not in")
        self.assertIn("akmoz", src[i:i + 200])

    def test_all_allowlists_cover_akmoz(self):
        """把所有写死来源名单的地方扫一遍，确保没有漏网的。

        这类白名单散落在 db 和 app 里，新增来源时漏一处就会出现
        "导入成功但看不到" 这种半截 bug。
        """
        import re
        checked = 0
        for rel in ("core/db.py", "webui/app.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            for m in re.finditer(
                    r'[\{\(]\s*"all",\s*"outlook"[^}\)]*[\}\)]', text):
                checked += 1
                self.assertIn("akmoz", m.group(0),
                              "%s 里有个来源名单漏了 akmoz：%s"
                              % (rel, m.group(0)[:90]))
        self.assertGreater(checked, 0, "没扫到任何来源名单，正则可能失效了")


class ApiTest(unittest.TestCase):
    def test_endpoint_accepts_akmoz(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        self.assertIn('"akmoz"', src)
        self.assertIn("db.import_akmoz_emails", src)

    def test_endpoint_uses_client_parser(self):
        """解析规则只写一份，别在 app.py 里再抄一遍。"""
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        self.assertIn("from core.akmoz_mail_client import parse_material", src)

    def test_pool_filter_allows_akmoz(self):
        """邮箱池列表能按 akmoz 筛选，否则导进去了也看不到。"""
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("_POOL_SOURCE_VALUES = ")
        self.assertIn("akmoz", src[i:i + 200])

    def test_email_change_allows_akmoz(self):
        """换绑邮箱时也能选 akmoz。"""
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index('allowed = {"outlook"')
        self.assertIn("akmoz", src[i:i + 260])


class UiTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "webui" / "templates" / "index.html").read_text(
            encoding="utf-8")

    def test_import_option_exists(self):
        self.assertIn('data-value="akmoz"', self.html)

    def test_import_label_defined(self):
        i = self.html.index("const IMPORT_SOURCE_LABELS")
        self.assertIn("akmoz", self.html[i:i + 300])

    def test_pool_filter_option_exists(self):
        i = self.html.index("const POOL_SOURCE_LABELS")
        self.assertIn("akmoz", self.html[i:i + 320])

    def test_placeholder_shows_format(self):
        """选了 akmoz 要提示格式，否则用户不知道该粘什么。"""
        i = self.html.index("text.placeholder")
        seg = self.html[i:i + 700]
        self.assertIn("akmoz", seg)
        self.assertIn("接码链接", seg)

    def test_modal_preselects_akmoz(self):
        """从 akmoz 池点导入时，类型要自动选中 akmoz。"""
        i = self.html.index("function openOutlookImportModal")
        seg = self.html[i:i + 500]
        self.assertIn("'akmoz'", seg)


if __name__ == "__main__":
    unittest.main()
