# -*- coding: utf-8 -*-
"""单席位 Team：探测与提链时自动使用。

Team 订阅默认最少 2 席，但服务端对部分账号放开了 1 席。
能用 1 席就省一半订阅费，所以 2FA 后自动探测、提链时自动使用。

三个实测结论，改动时别推翻：
  1. business_minimum_seats 只在带齐 5 个 Codex 头时才返回
  2. 它只在 GET 详情里，创建响应没有
  3. hosted 模式不接受 1 席，必须 custom + update
"""
import ast
import inspect
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "core" / "single_seat.py").read_text(encoding="utf-8")
TEAM = (ROOT / "core" / "team_checkout_service.py").read_text(encoding="utf-8")
TWOFA = (ROOT / "core" / "twofa_service.py").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
DB = (ROOT / "core" / "db.py").read_text(encoding="utf-8")


class HeaderTest(unittest.TestCase):
    """缺头会导致字段不返回，这是最容易踩的坑。"""

    def test_all_five_headers_present(self):
        from core.single_seat import CODEX_HEADERS
        for k in ("originator", "x-openai-web-frontend",
                  "x-openai-codex-window-type",
                  "x-openai-rate-limit-join-workspace",
                  "x-openai-checkout-source"):
            self.assertIn(k, CODEX_HEADERS, f"缺 {k} 会让 min 字段不返回")

    def test_why_documented(self):
        self.assertIn("别精简", SRC)

    def test_probe_uses_them(self):
        from core.single_seat import checkout_headers
        self.assertIn("CODEX_HEADERS", inspect.getsource(checkout_headers))


class DetailEndpointTest(unittest.TestCase):
    def test_ui_mode_must_be_custom(self):
        """hosted 模式下 business_minimum_seats 整个字段不返回（实测踩过）。"""
        from core.single_seat import build_create_payload
        p = build_create_payload("w", 2, "US", "USD")
        self.assertEqual(p.get("checkout_ui_mode"), "custom")
        self.assertIn("hosted 模式下服务端不返回", SRC)

    def test_reads_from_detail_not_create(self):
        """字段只在 GET 详情里，创建响应没有。"""
        from core.single_seat import read_minimum_seats
        src = inspect.getsource(read_minimum_seats)
        self.assertIn("CHECKOUT_DETAIL", src)
        self.assertIn("supports_business_seat_offer=true", src)

    def test_missing_field_returns_none_not_zero(self):
        """读不到要和"不支持"区分开，否则会误判。"""
        from core.single_seat import read_minimum_seats
        src = inspect.getsource(read_minimum_seats)
        self.assertIn("return None", src)


class ProbeTest(unittest.TestCase):
    def test_probe_does_not_pay_or_burn_promo(self):
        """探测只创建 session，不能改席位、不能传优惠码。"""
        from core.single_seat import probe_single_seat
        src = inspect.getsource(probe_single_seat)
        self.assertNotIn("update_seat_quantity", src)
        self.assertIn("不改席位、不支付", src)
        # build_create_payload 调用里不能带 promo
        i = src.index("build_create_payload")
        self.assertNotIn("promo", src[i:i + 200])

    def test_country_follows_exit(self):
        """账单国家必须和出口一致，否则创建直接 400。"""
        from core.single_seat import probe_single_seat
        src = inspect.getsource(probe_single_seat)
        self.assertIn("exit_geo", src)
        self.assertIn("COUNTRY_CURRENCY", src)

    def test_supported_means_min_le_one(self):
        from core.single_seat import probe_single_seat
        self.assertIn("minimum <= 1", inspect.getsource(probe_single_seat))


class CheckoutIntegrationTest(unittest.TestCase):
    def test_hosted_cannot_do_one_seat(self):
        """hosted 对任何账号都拒 1 席，必须 custom + update。"""
        # ui_mode 由 build_create_payload 统一给，这里确认默认就是 custom
        from core.single_seat import build_create_payload
        self.assertEqual(
            build_create_payload("w", 2, "US", "USD").get("checkout_ui_mode"),
            "custom")
        i = TEAM.index("def _try_single_seat")
        body = TEAM[i:i + 2200]
        self.assertIn("hosted 模式对任何账号", body)
        self.assertIn("update_seat_quantity", body)

    def test_falls_back_to_two_seats(self):
        """不支持、失败、异常都要退回 2 席，不能挡住提链。"""
        i = TEAM.index("def _try_single_seat")
        body = TEAM[i:i + 2600]
        self.assertGreaterEqual(body.count("return None"), 4)

    def test_probe_exception_does_not_break_checkout(self):
        i = TEAM.index("if _single_seat_enabled()")
        body = TEAM[i:i + 900]
        self.assertIn("except Exception", body)
        self.assertIn("按 2 席继续", body)

    def test_has_kill_switch(self):
        from config import register as cfg
        self.assertTrue(hasattr(cfg, "TEAM_TRY_SINGLE_SEAT"))
        self.assertIn("_single_seat_enabled", TEAM)

    def test_meta_reports_actual_seats(self):
        """命中时席位是 1，调用方据此判断该备多少钱。"""
        i = TEAM.index('"single_seat":')
        body = TEAM[max(0, i - 400):i + 120]
        self.assertIn('"seats"', body)


class TwoFaHookTest(unittest.TestCase):
    def test_called_after_success(self):
        i = TWOFA.index('"message": "2FA 设置完成"')
        body = TWOFA[i:i + 600]
        self.assertIn("_probe_single_seat_after_2fa", body)

    def test_runs_after_status_written(self):
        """先写 2FA 成功，再探测；顺序反了探测失败会盖掉结果。"""
        i = TWOFA.index("def _run_twofa")
        body = TWOFA[i:]
        self.assertLess(body.index("update_account_totp_secret"),
                        body.index("_probe_single_seat_after_2fa"))

    def test_never_breaks_2fa(self):
        """2FA 已经成功了，探测失败不能把结果搞成失败。"""
        src = TWOFA[TWOFA.index("def _probe_single_seat_after_2fa"):][:2000]
        self.assertIn("except Exception", src)
        self.assertNotIn("raise", src)

    def test_has_kill_switch(self):
        from config import register as cfg
        self.assertTrue(hasattr(cfg, "TEAM_PROBE_SINGLE_SEAT_AFTER_2FA"))


class StorageTest(unittest.TestCase):
    def test_failure_does_not_write_supported(self):
        """探测失败时不能把"没测出来"写成"不支持"。"""
        from core import db
        src = inspect.getsource(db.update_account_single_seat)
        tree = ast.parse(textwrap.dedent(src))
        fn = tree.body[0]
        code = ast.unparse(fn)
        i = code.index("if result.get('ok')")
        j = code.index("else:", i)
        self.assertIn("single_seat_supported", code[i:j])
        self.assertNotIn("single_seat_supported", code[j:])

    def test_fields_exposed_to_frontend(self):
        for f in ("single_seat_supported", "single_seat_minimum",
                  "single_seat_checked_at", "single_seat_error"):
            self.assertIn(f, DB, f"db 白名单缺 {f}")
            self.assertIn(f, APP, f"app 白名单缺 {f}")


class UiTest(unittest.TestCase):
    def test_route_exists(self):
        self.assertIn('"/api/accounts/<int:acc_id>/probe-single-seat"', APP)

    def test_tag_rendered_in_2fa_cell(self):
        self.assertIn("_singleSeatTag", HTML)
        i = HTML.index("function _totpCellV2")
        self.assertIn("_singleSeatTag(r)", HTML[i:i + 1800])

    def test_distinguishes_unknown_from_unsupported(self):
        """没探测过 ≠ 不支持，必须区分显示。"""
        i = HTML.index("function _singleSeatTag")
        body = HTML[i:i + 1400]
        self.assertIn("=== true", body)
        self.assertIn("=== false", body)
        self.assertIn("data-probe-single-seat", body)

    def test_manual_probe_bound(self):
        self.assertIn("closest('[data-probe-single-seat]')", HTML)
        self.assertIn("async function probeSingleSeat", HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
