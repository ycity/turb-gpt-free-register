# -*- coding: utf-8 -*-
"""邮箱连续失败要熔断，不能把整个自动注册队列卡死。

线上事故（2026-11-02~03）：领取按 id 升序取第一个 available，一个邮箱
注册失败后被放回 available，下一轮必然又被选中，连续 28 轮全是它，
后面的邮箱一个都没轮到。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402


class ReleaseFailureCountTest(unittest.TestCase):
    def setUp(self):
        self.rows = [{
            "id": 39, "email": "a@e.test", "status": "used",
            "password": "p", "client_id": "c", "refresh_token": "r",
        }]
        self.saved = []

    def _release(self, note="boom"):
        with patch.object(db, "_load_accounts", return_value=[]), \
             patch.object(db, "_load_outlook", return_value=self.rows), \
             patch.object(db, "_save_outlook", side_effect=self.saved.append):
            return db.release_unconsumed_outlook("a@e.test", note=note)

    def test_first_failures_go_back_to_pool(self):
        self.assertTrue(self._release())
        self.assertEqual(self.rows[0]["status"], "available")
        self.assertEqual(self.rows[0]["consecutive_failures"], 1)

    def test_counter_accumulates(self):
        for expect in (1, 2):
            self.rows[0]["status"] = "used"
            self._release()
            self.assertEqual(self.rows[0]["consecutive_failures"], expect)
            self.assertEqual(self.rows[0]["status"], "available")

    def test_disabled_after_limit(self):
        """达到上限必须停用，否则它会一直霸占队列。"""
        for _ in range(db._EMAIL_MAX_CONSECUTIVE_FAILURES):
            self.rows[0]["status"] = "used"
            self._release()
        self.assertEqual(self.rows[0]["status"], "disabled")
        self.assertIn("连续失败", self.rows[0]["note"])

    def test_disabled_email_is_not_claimable(self):
        self.rows[0]["status"] = "disabled"
        with patch.object(db, "_load_outlook", return_value=self.rows), \
             patch.object(db, "_save_outlook"), \
             patch.object(db, "_decorate_outlook", side_effect=lambda r: r):
            self.assertIsNone(db.claim_next_outlook())

    def test_note_shows_progress(self):
        self._release(note="页面超时")
        self.assertIn(f"1/{db._EMAIL_MAX_CONSECUTIVE_FAILURES}", self.rows[0]["note"])
        self.assertIn("页面超时", self.rows[0]["note"])

    def test_already_registered_email_is_untouched(self):
        with patch.object(db, "_load_accounts",
                          return_value=[{"email": "a@e.test"}]), \
             patch.object(db, "_load_outlook", return_value=self.rows), \
             patch.object(db, "_save_outlook") as save:
            self.assertFalse(db.release_unconsumed_outlook("a@e.test"))
        save.assert_not_called()

    def test_corrupt_counter_does_not_crash(self):
        self.rows[0]["consecutive_failures"] = "oops"
        self.assertTrue(self._release())
        self.assertEqual(self.rows[0]["consecutive_failures"], 1)


class ClearFailuresTest(unittest.TestCase):
    def test_success_resets_counter(self):
        rows = [{"id": 1, "email": "a@e.test", "consecutive_failures": 2}]
        with patch.object(db, "_load_outlook", return_value=rows), \
             patch.object(db, "_save_outlook"):
            self.assertTrue(db.clear_outlook_failures("a@e.test"))
        self.assertEqual(rows[0]["consecutive_failures"], 0)

    def test_noop_when_no_failures(self):
        rows = [{"id": 1, "email": "a@e.test"}]
        with patch.object(db, "_load_outlook", return_value=rows), \
             patch.object(db, "_save_outlook") as save:
            self.assertFalse(db.clear_outlook_failures("a@e.test"))
        save.assert_not_called()

    def test_unknown_email_is_safe(self):
        with patch.object(db, "_load_outlook", return_value=[]), \
             patch.object(db, "_save_outlook") as save:
            self.assertFalse(db.clear_outlook_failures("nobody@e.test"))
        save.assert_not_called()


class QueueNotBlockedTest(unittest.TestCase):
    def test_bad_email_eventually_yields_to_next(self):
        """熔断后，后面的邮箱必须能被领到。"""
        rows = [
            {"id": 39, "email": "bad@e.test", "status": "used",
             "password": "p", "client_id": "c", "refresh_token": "r"},
            {"id": 40, "email": "good@e.test", "status": "available",
             "password": "p", "client_id": "c", "refresh_token": "r"},
        ]
        for _ in range(db._EMAIL_MAX_CONSECUTIVE_FAILURES):
            rows[0]["status"] = "used"
            with patch.object(db, "_load_accounts", return_value=[]), \
                 patch.object(db, "_load_outlook", return_value=rows), \
                 patch.object(db, "_save_outlook"):
                db.release_unconsumed_outlook("bad@e.test", note="x")

        self.assertEqual(rows[0]["status"], "disabled")
        with patch.object(db, "_load_outlook", return_value=rows), \
             patch.object(db, "_save_outlook"), \
             patch.object(db, "_decorate_outlook", side_effect=lambda r: r):
            got = db.claim_next_outlook()
        self.assertIsNotNone(got)
        self.assertEqual(got["email"], "good@e.test")


if __name__ == "__main__":
    unittest.main()
