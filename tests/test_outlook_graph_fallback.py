# -*- coding: utf-8 -*-
"""远端取信挂掉时，auto 模式必须切到 Graph 直连。

事故（2026-11-05）：mail.chatai.codes 跑在 Cloudflare Workers 免费运行时上，
/api/fetch-imap 固定返回 HTTP 501 IMAP_REQUIRES_CONTAINER。两个 bug 叠加：

1. _is_remote_disabled_error 只认 402 / DEPLOYMENT_DISABLED，不认 501，
   所以 auto 模式压根不觉得远端挂了，一轮轮重试同一个必失败的接口。
2. 就算认出来了，回退分支写成 `if protocol == "graph"`，而失败的是 imap，
   于是只置了标记就 return []，本轮仍然空手而归。

结果：#61 / #53 的查活都卡在"等待 OTP 超时"，拿不到验证码，
把"账号可能被封"这件事掩盖成了"邮箱超时"。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "core" / "outlook_client.py").read_text(encoding="utf-8")


class RemoteDisabledDetectionTest(unittest.TestCase):
    def test_recognises_imap_requires_container(self):
        from core.outlook_client import _is_remote_disabled_error
        body = ('{"success":false,"protocol":"imap","code":"IMAP_REQUIRES_CONTAINER",'
                '"error":"Cloudflare Workers 免费运行时不支持当前 IMAP TCP/TLS 实现"}')
        self.assertTrue(_is_remote_disabled_error(body))

    def test_recognises_http_501(self):
        from core.outlook_client import _is_remote_disabled_error
        self.assertTrue(_is_remote_disabled_error(
            "secure_post https://mail.chatai.codes/api/fetch-imap HTTP 501: ..."))

    def test_still_recognises_payment_errors(self):
        """原有的欠费判断不能丢。"""
        from core.outlook_client import _is_remote_disabled_error
        self.assertTrue(_is_remote_disabled_error("DEPLOYMENT_DISABLED"))
        self.assertTrue(_is_remote_disabled_error("HTTP 402"))
        self.assertTrue(_is_remote_disabled_error("Payment required"))

    def test_ordinary_errors_do_not_trigger_switch(self):
        """超时、500 这类临时故障不该让整个进程放弃远端。"""
        from core.outlook_client import _is_remote_disabled_error
        for text in ("HTTP 500", "timeout", "connection reset",
                     "HTTP 403 SECURITY_ENVELOPE_INVALID", ""):
            self.assertFalse(_is_remote_disabled_error(text), text)

    def test_accepts_exception_object(self):
        from core.outlook_client import (_is_remote_disabled_error,
                                         OutlookClientError)
        exc = OutlookClientError("... HTTP 501: IMAP_REQUIRES_CONTAINER ...")
        self.assertTrue(_is_remote_disabled_error(exc))


class FallbackWiringTest(unittest.TestCase):
    def _fetch_block(self):
        idx = SRC.index('url = f"{OUTLOOK_API_BASE.rstrip(\'/\')}/api/fetch-{protocol}"')
        return SRC[idx:idx + 1400]

    def test_falls_back_regardless_of_protocol(self):
        block = self._fetch_block()
        self.assertIn("_REMOTE_DISABLED = True", block)
        self.assertIn("return _fetch_via_graph_direct(account)", block)

    def test_no_longer_gated_on_graph_protocol(self):
        """原来写成 if protocol == 'graph' 才回退，imap 失败时等于没救。"""
        block = self._fetch_block()
        self.assertNotIn('if protocol == "graph":', block)

    def test_reason_documented(self):
        block = self._fetch_block()
        self.assertIn("501", block)


class DocumentationTest(unittest.TestCase):
    def test_incident_explained_in_source(self):
        idx = SRC.index("def _is_remote_disabled_error")
        doc = SRC[idx:idx + 900]
        self.assertIn("IMAP_REQUIRES_CONTAINER", doc)
        self.assertIn("Cloudflare Workers", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
