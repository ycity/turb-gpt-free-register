# -*- coding: utf-8 -*-
"""账单地址簿与支付配置覆盖。

不碰网络、不开浏览器、不动钱。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class BillingStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp.name) / "t.sqlite3")
        import core.billing_address_store as store
        self.store = store
        self._orig = store.DB_PATH
        store.DB_PATH = self._db

    def tearDown(self):
        self.store.DB_PATH = self._orig
        self._tmp.cleanup()

    ADDR = {
        "name": "ONeal Di", "country": "us", "state": "AK",
        "city": "Anchorage", "line1": "750 West Dimond Boulevard",
        "line2": "", "postal": "99515",
    }

    def test_save_and_list(self):
        saved = self.store.save_address(self.ADDR)
        self.assertTrue(saved["id"])
        self.assertEqual(saved["country"], "US")      # 国家统一大写
        rows = self.store.list_addresses()
        self.assertEqual(len(rows), 1)
        self.assertIn("750 West Dimond Boulevard", rows[0]["label"])

    def test_same_address_is_not_duplicated(self):
        first = self.store.save_address(self.ADDR)
        again = self.store.save_address({**self.ADDR, "city": " anchorage "})
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.store.list_addresses()), 1)

    def test_different_address_creates_new_row(self):
        self.store.save_address(self.ADDR)
        self.store.save_address({**self.ADDR, "line1": "11901 Northern Raven Dr",
                                 "postal": "99516"})
        self.assertEqual(len(self.store.list_addresses()), 2)

    def test_missing_required_field_rejected(self):
        for key in ("country", "city", "line1"):
            with self.assertRaises(ValueError):
                self.store.save_address({**self.ADDR, key: ""})

    def test_recently_used_sorts_first(self):
        a = self.store.save_address(self.ADDR)
        b = self.store.save_address({**self.ADDR, "line1": "1 Other St"})
        self.store.mark_used(a["id"])
        self.assertEqual(self.store.list_addresses()[0]["id"], a["id"])
        self.store.mark_used(b["id"])
        self.assertEqual(self.store.list_addresses()[0]["id"], b["id"])

    def test_delete(self):
        saved = self.store.save_address(self.ADDR)
        self.assertTrue(self.store.delete_address(saved["id"]))
        self.assertEqual(self.store.list_addresses(), [])
        self.assertFalse(self.store.delete_address(saved["id"]))


class BuildPayCfgTest(unittest.TestCase):
    """弹窗选的卡和地址要覆盖配置，且不能污染全局模块。"""

    @staticmethod
    def _base():
        from config import vcc
        return vcc

    def test_overrides_card_and_billing(self):
        from webui.app import _build_pay_cfg
        cfg = _build_pay_cfg(self._base(), {
            "card_id": "CARD123",
            "holder_id": 1838,
            "billing": {"country": "us", "state": "CA", "city": "San Jose",
                        "line1": "1 Infinite Loop", "postal": "95014",
                        "name": "Test Holder"},
        })
        self.assertEqual(cfg.VCC_CARD_ID, "CARD123")
        self.assertEqual(cfg.VCC_HOLDER_ID, 1838)
        self.assertEqual(cfg.VCC_BILLING_COUNTRY, "US")
        self.assertEqual(cfg.VCC_BILLING_CITY, "San Jose")
        self.assertEqual(cfg.VCC_NAME_SOURCE, "config")

    def test_global_config_untouched(self):
        from config import vcc
        from webui.app import _build_pay_cfg
        before = vcc.VCC_BILLING_CITY
        _build_pay_cfg(vcc, {"billing": {"city": "Somewhere Else",
                                         "country": "US", "line1": "x"}})
        self.assertEqual(vcc.VCC_BILLING_CITY, before)

    def test_empty_payload_keeps_defaults(self):
        from config import vcc
        from webui.app import _build_pay_cfg
        cfg = _build_pay_cfg(vcc, {})
        self.assertEqual(cfg.VCC_BILLING_CITY, vcc.VCC_BILLING_CITY)
        self.assertEqual(cfg.VCC_EXPECTED_AMOUNT, vcc.VCC_EXPECTED_AMOUNT)

    def test_blank_name_keeps_holder_source(self):
        """没填姓名时仍按持卡人取，不要退化成空字符串。"""
        from config import vcc
        from webui.app import _build_pay_cfg
        cfg = _build_pay_cfg(vcc, {"billing": {"country": "US", "city": "A",
                                               "line1": "B", "name": ""}})
        self.assertEqual(cfg.VCC_NAME_SOURCE, vcc.VCC_NAME_SOURCE)

    def test_gate_settings_are_not_overridable(self):
        """闸门是资金安全线，不能被请求体改掉。"""
        from config import vcc
        from webui.app import _build_pay_cfg
        cfg = _build_pay_cfg(vcc, {
            "VCC_EXPECTED_AMOUNT": 99999,
            "VCC_MAX_CHARGE": 99999,
            "expected_amount": 99999,
            "max_charge": 99999,
        })
        self.assertEqual(cfg.VCC_EXPECTED_AMOUNT, vcc.VCC_EXPECTED_AMOUNT)
        self.assertEqual(cfg.VCC_MAX_CHARGE, vcc.VCC_MAX_CHARGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
