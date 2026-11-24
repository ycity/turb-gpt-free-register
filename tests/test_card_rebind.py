# -*- coding: utf-8 -*-
"""换绑支付卡（admin.openai.com 账单页）的接口与服务测试。

背景（2026-11-19 实测）：换绑 = 管理后台加新卡（Stripe Elements +
hCaptcha Enterprise 自动打码）→ 设默认 → 删旧卡；默认目标卡取
AUTO_SUBSCRIBE_CARD_BLACKLIST 黑名单牺牲卡。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402

SEED = [{
    "id": 9201,
    "email": "rebind-test@outlook.com",
    "access_token": "tok",
    "email_source": "outlook",
    "paid_at": "2026-11-19T00:00:00",
    "pay_card_label": "VCC-5762",
    "paid_workspace_account_id": "ws-1234",
    "registration_password": "Pwd!AlreadySet123",
    "totp_secret": "ABC",
    "extra_json": json.dumps({"user": {"email": "rebind-test@outlook.com"}}),
}]


class _TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        accounts_path = root / "accounts.json"
        accounts_path.write_text(json.dumps(SEED), encoding="utf-8")
        patches = [
            patch.object(db, "_ACCOUNTS_JSON", accounts_path),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy_accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
            patch.object(db, "_GENERIC_API_EMAIL_JSON", root / "api_emails.json"),
            patch.object(db, "_GENERIC_API_EMAIL_TXT", root / "api_emails.txt"),
            patch.object(db, "_JOBS_JSON", root / "jobs.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.acc_id = 9201
        self.assertIsNotNone(db.get_account(self.acc_id))


class DbRebindStateTest(_TempDbTestCase):
    def test_claim_stale_and_recover(self):
        self.assertTrue(db.claim_account_card_rebind(self.acc_id, "manual"))
        self.assertFalse(db.claim_account_card_rebind(self.acc_id, "manual"))
        self.assertTrue(db.mark_account_card_rebind_running(self.acc_id))
        interrupted = db.recover_interrupted_card_rebinds()
        self.assertEqual(len(interrupted), 1)
        row = db.get_account(self.acc_id)
        self.assertEqual(row["card_rebind_status"], "failed")
        self.assertIn("中断", row["card_rebind_error"])

    def test_success_forces_pay_card_label(self):
        """换绑成功必须覆盖 pay_card_label——换绑后的扣款卡才是对账要看的。"""
        db.claim_account_card_rebind(self.acc_id, "manual")
        ok = db.update_account_card_rebind(self.acc_id, {
            "ok": True, "status": "success", "card_label": "VCC-5931",
            "message": "已换绑到 VCC-5931"})
        self.assertTrue(ok)
        row = db.get_account(self.acc_id)
        self.assertEqual(row["card_rebind_status"], "success")
        self.assertEqual(row["pay_card_label"], "VCC-5931",
                         "换绑成功后 pay_card_label 没有被覆盖")

    def test_first_pay_semantics_still_no_overwrite(self):
        """force 默认 False：首次支付记录的语义不变。"""
        self.assertFalse(db.set_account_pay_card(self.acc_id, "VCC-9999"))
        self.assertEqual(db.get_account(self.acc_id)["pay_card_label"], "VCC-5762")
        self.assertTrue(db.set_account_pay_card(self.acc_id, "VCC-8888", force=True))

    def test_failure_keeps_old_label(self):
        db.claim_account_card_rebind(self.acc_id, "manual")
        db.update_account_card_rebind(self.acc_id, {"ok": False, "status": "failed", "error": "boom"})
        row = db.get_account(self.acc_id)
        self.assertEqual(row["card_rebind_status"], "failed")
        self.assertEqual(row["pay_card_label"], "VCC-5762", "失败不该动扣款卡记录")

    def test_status_projection(self):
        db.claim_account_card_rebind(self.acc_id, "manual")
        snapshot = db.list_account_plan_check_statuses()
        item = next(x for x in snapshot["items"] if x["id"] == self.acc_id)
        self.assertEqual(item.get("card_rebind_status"), "queued")


class TargetCardTest(unittest.TestCase):
    def test_blacklist_default_prefers_active_blacklisted(self):
        """未指定卡时取黑名单里第一张 ACTIVE 的 VCC 卡。"""
        from core import card_rebind_service as svc
        client = MagicMock()
        client._is_active.side_effect = lambda c: c.get("cardStatus") == "ACTIVE"
        client.list_cards.return_value = [
            {"cardId": "c1", "cardNoMask": "****1111", "last4": "1111", "cardStatus": "ACTIVE"},
            {"cardId": "c2", "cardNoMask": "****5931", "last4": "5931", "cardStatus": "ACTIVE"},
            {"cardId": "c3", "cardNoMask": "****8361", "last4": "8361", "cardStatus": "FROZEN"},
        ]
        client.card_secrets.return_value = {
            "number": "5111111111115931", "exp_month": "12", "exp_year": "2030", "cvc": "123"}
        with patch("core.vcc_client.build_client_from_config", return_value=client), \
             patch.object(svc, "_blacklist_last4s", return_value={"5931", "8361"}):
            card = svc.resolve_target_card("")
        self.assertEqual(card["card_id"], "c2")
        self.assertEqual(card["label"], "VCC-5931")
        self.assertEqual(card["last4"], "5931")
        self.assertEqual(card["exp_year"], "30", "Stripe 年份框要两位")

    def test_no_blacklist_card_active_raises(self):
        from core import card_rebind_service as svc
        client = MagicMock()
        client._is_active.return_value = False
        client.list_cards.return_value = []
        with patch("core.vcc_client.build_client_from_config", return_value=client), \
             patch.object(svc, "_blacklist_last4s", return_value={"5931"}):
            with self.assertRaises(ValueError):
                svc.resolve_target_card("")


class AdminBillingPageTest(unittest.TestCase):
    def test_billing_url(self):
        from core.admin_billing import billing_url
        self.assertEqual(
            billing_url("ws-1", "MyTeam"),
            "https://admin.openai.com/workspace/ws-1/billing?workspace=MyTeam")
        self.assertEqual(
            billing_url("ws-1"),
            "https://admin.openai.com/workspace/ws-1/billing?workspace=myteam")

    def test_read_payment_methods_parses_text(self):
        from core import admin_billing as ab
        page = MagicMock()
        page.evaluate.return_value = (
            "お支払い方法\n新しく追加\n\nMastercard\n•••• 5762\nデフォルト\n"
            "予備のお支払い方法を使用する\n\nVisa\n•••• 5931\n"
        )
        cards = ab.read_payment_methods(page)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["last4"], "5762")
        self.assertTrue(cards[0]["default"])
        self.assertEqual(cards[0]["brand"], "Mastercard")
        self.assertEqual(cards[1]["last4"], "5931")
        self.assertFalse(cards[1]["default"])

    def test_sso_requires_password_when_asked(self):
        """SSO 停在密码页而账号没密码时，要明确报错而不是卡住。"""
        from core import admin_billing as ab
        page = MagicMock()
        page.url = "https://auth.openai.com/log-in/password"
        page.goto = MagicMock()
        with self.assertRaises(RuntimeError):
            ab.sso_into_admin(page, "a@b.test", password="",
                              target_url="https://admin.openai.com/x")

    def test_sitekey_from_stripecdn_url(self):
        from core import admin_billing as ab
        page = MagicMock()
        f = MagicMock()
        f.url = ("https://b.stripecdn.com/stripethirdparty-srv/assets/v37.5/"
                 "HCaptchaInvisible.html?id=aeab9d52-8f02-4ed9-abc3-ca16bcde4041&origin=x")
        page.frames = [f]
        page.url = "https://admin.openai.com/workspace/ws/billing"
        sitekey, pageurl = ab._extract_hcaptcha_sitekey(page)
        self.assertEqual(sitekey, "aeab9d52-8f02-4ed9-abc3-ca16bcde4041")
        self.assertIn("admin.openai.com", pageurl)


class EnqueueGuardTest(unittest.TestCase):
    def test_empty_email_rejected(self):
        from core import card_rebind_service as svc
        r = svc.enqueue_account_card_rebind(account_id=1, email="")
        self.assertFalse(r["accepted"])

    def test_busy_when_claimed(self):
        from core import card_rebind_service as svc
        with patch.object(svc, "apply_settings", return_value={}), \
             patch("core.db.claim_account_card_rebind", return_value=False) as claim:
            r = svc.enqueue_account_card_rebind(account_id=7, email="a@b.test", card_id="x")
        self.assertFalse(r["accepted"])
        self.assertTrue(r["busy"])
        claim.assert_called_once()


if __name__ == "__main__":
    unittest.main()
