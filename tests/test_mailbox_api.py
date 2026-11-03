# -*- coding: utf-8 -*-
"""简易收件箱：列表 + 正文。

原先只有一个"收验证码"按钮弹个 6 位数，看不到邮件原文（安全提醒、改密通知
都看不了）。改成左列表右详情的收件箱。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HTML = (Path(__file__).resolve().parents[1]
        / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


def _app():
    import os
    os.environ.setdefault("WEBUI_AUTH_CODE", "t-code")
    from webui.app import create_app
    return create_app(auth_code="t-code")


HEAD = {"X-Auth-Code": "t-code", "Content-Type": "application/json"}

SAMPLE = [{
    "id": "m1", "subject": "Your temporary ChatGPT login code",
    "from_name": "OpenAI", "from_email": "noreply@openai.com",
    "date": "2026-11-03T11:07:18Z", "preview": "code 030857",
    "html": "<p>030857</p>", "otp": "030857",
}]


class MailboxApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _app().test_client()

    def _post(self, payload):
        return self.client.post("/api/mailbox/list", headers=HEAD,
                                data=json.dumps(payload))

    def test_requires_identifier(self):
        r = self._post({})
        self.assertEqual(r.status_code, 400)

    def test_returns_mail_list(self):
        with patch("core.email_provider.resolve_email_source", return_value="outlook"), \
             patch("core.outlook_client.list_inbox", return_value=SAMPLE):
            r = self._post({"email": "a@outlook.com"})
        b = r.get_json()
        self.assertTrue(b["ok"])
        self.assertEqual(b["count"], 1)
        self.assertEqual(b["mails"][0]["otp"], "030857")
        self.assertIn("html", b["mails"][0])

    def test_resolves_email_from_account_id(self):
        with patch("webui.app.db.get_account",
                   return_value={"id": 7, "email": "db@outlook.com"}), \
             patch("core.email_provider.resolve_email_source", return_value="outlook"), \
             patch("core.outlook_client.list_inbox", return_value=[]) as li:
            r = self._post({"account_id": 7})
        self.assertEqual(r.get_json()["email"], "db@outlook.com")
        self.assertEqual(li.call_args.args[0], "db@outlook.com")

    def test_non_outlook_source_is_rejected_clearly(self):
        with patch("core.email_provider.resolve_email_source", return_value="mailnest"):
            r = self._post({"email": "a@x.test"})
        b = r.get_json()
        self.assertFalse(b["ok"])
        self.assertIn("mailnest", b["error"])

    def test_fetch_failure_is_reported_not_500(self):
        with patch("core.email_provider.resolve_email_source", return_value="outlook"), \
             patch("core.outlook_client.list_inbox",
                   side_effect=RuntimeError("refresh_token 失效")):
            r = self._post({"email": "a@outlook.com"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["ok"])
        self.assertIn("refresh_token", r.get_json()["error"])

    def test_limit_is_clamped(self):
        with patch("core.email_provider.resolve_email_source", return_value="outlook"), \
             patch("core.outlook_client.list_inbox", return_value=[]) as li:
            self._post({"email": "a@outlook.com", "limit": 9999})
            self.assertLessEqual(li.call_args.kwargs["limit"], 100)

    def test_bad_limit_rejected(self):
        r = self._post({"email": "a@outlook.com", "limit": "abc"})
        self.assertEqual(r.status_code, 400)


class FrontendTest(unittest.TestCase):
    def test_modal_and_handler_present(self):
        for token in ("mailboxModal", "openMailbox", "/api/mailbox/list",
                      "mbx-list", "mbx-detail", "收件箱"):
            self.assertIn(token, HTML, f"缺少 {token}")

    def test_body_rendered_in_sandboxed_iframe(self):
        """邮件正文是外部 HTML，必须隔离，不能直接 innerHTML 进页面。"""
        seg = HTML[HTML.index("function _mailboxShow"):]
        seg = seg[:seg.index("async function _mailboxLoad")]
        self.assertIn("sandbox", seg)
        self.assertIn("srcdoc", seg)

    def test_old_crude_button_removed(self):
        self.assertNotIn("fetchAccountOtp", HTML)


class FieldMappingTest(unittest.TestCase):
    """各通道字段名不统一，正文不能因此丢掉。"""

    def _rows(self, raw):
        from core import outlook_client as oc
        with patch.object(oc, "get_account_context", return_value=object()), \
             patch.object(oc, "_http_session", return_value=object()), \
             patch.object(oc, "_fetch_via", return_value=raw):
            return oc.list_inbox("a@outlook.com", limit=10)

    def test_remote_bodyhtml_is_kept(self):
        """线上实测踩到的坑：远端返回 bodyHtml，没有 receivedDateTime，
        先过 _normalize_ms_message 会把正文丢成空。"""
        rows = self._rows([{
            "bodyHtml": "<p>hello</p>", "bodyText": "hello",
            "bodyPreview": "hello", "date": "2026-11-03T11:10:02Z",
            "subject": "s", "from": "noreply@tm.openai.com",
            "fromName": "OpenAI", "id": "i1",
        }])
        self.assertEqual(rows[0]["html"], "<p>hello</p>")
        self.assertEqual(rows[0]["from_email"], "noreply@tm.openai.com")
        self.assertEqual(rows[0]["date"], "2026-11-03T11:10:02Z")

    def test_graph_body_content_is_kept(self):
        rows = self._rows([{
            "body": {"content": "<b>hi</b>", "contentType": "html"},
            "receivedDateTime": "2026-11-03T10:00:00Z", "subject": "s2",
            "from": {"emailAddress": {"address": "x@y.z", "name": "X"}},
            "id": "i2",
        }])
        self.assertEqual(rows[0]["html"], "<b>hi</b>")
        self.assertEqual(rows[0]["from_email"], "x@y.z")

    def test_text_only_mail_still_has_something_to_show(self):
        rows = self._rows([{
            "bodyText": "plain only", "date": "2026-11-03T09:00:00Z",
            "subject": "s3", "id": "i3",
        }])
        self.assertEqual(rows[0]["html"], "")
        self.assertEqual(rows[0]["text"], "plain only")
        self.assertEqual(rows[0]["preview"], "plain only")

    def test_sorted_newest_first(self):
        rows = self._rows([
            {"subject": "old", "date": "2026-11-01T00:00:00Z", "id": "a"},
            {"subject": "new", "date": "2026-11-03T00:00:00Z", "id": "b"},
        ])
        self.assertEqual([r["subject"] for r in rows], ["new", "old"])

    def test_frontend_falls_back_to_text(self):
        seg = HTML[HTML.index("function _mailboxShow"):]
        seg = seg[:seg.index("async function _mailboxLoad")]
        self.assertIn("m.text", seg, "没有 html 的邮件要退回纯文本显示")
