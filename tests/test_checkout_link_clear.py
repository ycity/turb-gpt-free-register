# -*- coding: utf-8 -*-
"""支付成功后清掉长链。

长链付过一次就作废。不清的话下次点支付会提示"已有长链，可复用"，
拿它再付必然失败，还白等一轮 30 秒探测。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
SESSION = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")


class DbClearTest(unittest.TestCase):
    def test_clear_removes_all_three_fields(self):
        """只清 url 不够：created_at 还在的话前端仍会显示"已有长链"。"""
        import core.db as db
        src = __import__("inspect").getsource(db.update_account_team_checkout)
        self.assertIn("clear", src)
        for field in ("team_checkout_url", "team_checkout_created_at",
                      "team_checkout_error"):
            self.assertIn(field, src)

    def test_clear_is_keyword_only(self):
        """避免和既有的位置参数 result 混淆。"""
        import inspect

        import core.db as db
        sig = inspect.signature(db.update_account_team_checkout)
        p = sig.parameters["clear"]
        self.assertIs(p.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(p.default, False)

    def test_write_path_unchanged(self):
        """不传 clear 时行为必须和以前一样，否则生成长链会被清掉。"""
        import core.db as db
        src = __import__("inspect").getsource(db.update_account_team_checkout)
        self.assertIn('if result.get("url")', src)


class AutoPayClearTest(unittest.TestCase):
    def test_called_after_done(self):
        idx = SESSION.index("state=ST_DONE")
        body = SESSION[idx:idx + 400]
        self.assertIn("_clear_checkout_link(sess)", body)

    def test_runs_after_paid_at_recorded(self):
        """先记时间再清链：清链失败也不能丢掉支付时间。"""
        idx = SESSION.index("state=ST_DONE")
        body = SESSION[idx:idx + 400]
        self.assertLess(body.index("_record_paid_at(sess)"),
                        body.index("_clear_checkout_link(sess)"))

    def test_failure_does_not_break_payment(self):
        """钱已经付了，清链失败不能把会话判成失败。"""
        idx = SESSION.index("def _clear_checkout_link")
        body = SESSION[idx:idx + 900]
        self.assertIn("except Exception", body)
        self.assertNotIn("raise", body)


class ManualPayClearTest(unittest.TestCase):
    def test_manual_done_clears_link(self):
        idx = APP.index("def api_pay_manual_done")
        body = APP[idx:idx + 3000]
        self.assertIn("clear=True", body)

    def test_only_clears_when_link_exists(self):
        """没长链时不该报"已清掉"，那是假消息。"""
        idx = APP.index("def api_pay_manual_done")
        body = APP[idx:idx + 3000]
        i = body.index("clear=True")
        self.assertIn('acc.get("team_checkout_url")', body[max(0, i - 300):i])


if __name__ == "__main__":
    unittest.main(verbosity=2)
