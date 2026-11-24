# -*- coding: utf-8 -*-
"""akmoz 素材导入为账号（不是导入邮箱池）。

2026-11-19 事故：用户给 10 个 akmoz 账号要求"补进账号池"，我按邮箱池导入了
（import_akmoz_emails），只进了邮箱池、账号页看不到，属于导错了地方。

akmoz 素材是 3 段：email----password----code_url。而
import_registered_email_accounts 原来只认 outlook/generic_api/imap，
且 WebUI 的 akmoz 分支在 as_registered=True 时又落回 outlook 的 4 段解析
（len(parts) < 4 直接 continue），所以 akmoz 账号根本导不进账号池。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class ImportSourceTest(unittest.TestCase):
    def test_akmoz_is_accepted(self):
        from core import db
        # 不该再抛 "source 必须显式传入"
        try:
            db.import_registered_email_accounts([], source="akmoz")
        except ValueError as exc:
            self.fail(f"akmoz 应被接受，却抛了：{exc}")

    def test_bad_source_still_rejected(self):
        from core import db
        with self.assertRaises(ValueError):
            db.import_registered_email_accounts([], source="nope")

    def test_source_doc_mentions_akmoz(self):
        from core import db
        doc = (db.import_registered_email_accounts.__doc__ or "").lower()
        self.assertIn("akmoz", doc)


class WebUiDispatchTest(unittest.TestCase):
    """WebUI 的 akmoz 分支必须两条路都走 parse_material。"""

    def setUp(self):
        self.src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")

    def test_akmoz_branch_not_gated_on_as_registered(self):
        i = self.src.index('if source == "akmoz":')
        seg = self.src[i:i + 1600]
        # 不能再出现 `source == "akmoz" and not as_registered`
        self.assertNotIn('source == "akmoz" and not as_registered', self.src)
        self.assertIn("parse_material", seg)

    def test_akmoz_as_registered_goes_to_account_import(self):
        i = self.src.index('if source == "akmoz":')
        seg = self.src[i:i + 1600]
        self.assertIn("if as_registered:", seg)
        self.assertIn("import_registered_email_accounts", seg)
        self.assertIn('source="akmoz"', seg)

    def test_akmoz_email_import_still_available(self):
        """不是账号导入时仍要能只进邮箱池。"""
        i = self.src.index('if source == "akmoz":')
        seg = self.src[i:i + 1600]
        self.assertIn("import_akmoz_emails", seg)


class AkmozPoolRowTest(unittest.TestCase):
    """导入成账号后，akmoz 池里的行要标 used 并留住密码。"""

    def test_akmoz_pool_row_keeps_password_and_code_url(self):
        from core import db
        rec = {"email": "a@b.test", "password": "pwd123",
               "code_url": "https://x/email_query?email=a%40b.test"}
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_outlook", return_value=[]), \
             patch.object(db, "_load_generic_api_emails", return_value=[]), \
             patch.object(db, "_load_imap_emails", return_value=[]), \
             patch.object(db, "_load_akmoz_emails", return_value=[]) as load_ak, \
             patch.object(db, "_save_akmoz_emails") as save_ak, \
             patch.object(db, "_save_accounts"), \
             patch.object(db, "_save_outlook"), \
             patch.object(db, "_save_generic_api_emails"), \
             patch.object(db, "_save_imap_emails"), \
             patch.object(db, "_next_account_id", return_value=99):
            rows = []
            load_ak.return_value = rows
            inserted, skipped = db.import_registered_email_accounts(
                [rec], source="akmoz")
        self.assertEqual((inserted, skipped), (1, 0))
        save_ak.assert_called_once()
        saved = save_ak.call_args[0][0]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["email"], "a@b.test")
        self.assertEqual(saved[0]["password"], "pwd123")
        self.assertEqual(saved[0]["code_url"], rec["code_url"])
        self.assertEqual(saved[0]["status"], "used")


if __name__ == "__main__":
    unittest.main()
