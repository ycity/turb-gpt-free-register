# -*- coding: utf-8 -*-
"""优惠码只在支付成功后才算消耗。

问题（2026-11-18 发现）：
  generate_team_checkout_link 拿到长链就把码标成 used，但支付是后续独立
  步骤。支付失败时 OpenAI 侧并没有消耗这个码，池里却已记成 used，一个
  还能用的码被永久埋掉。

  当天调试自动订阅白埋 3 个：
    YRR5GDKJENMZLL4V  等待确认超时被回收
    ZA37ASS8YZDK2GYV  金额闸门拦下 993.60
    ZJ6M59EVVBEFSZUQ  金额闸门再次拦下（脏地址卡）

修法：提链只标 using，支付真正提交成功才升 used；
     卡在 using 超过一定时间的由 reclaim_stale_promo_codes 退回 unused。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class LinkDoesNotConsumeTest(unittest.TestCase):
    """提链不等于消耗。"""

    def test_marks_using_not_used(self):
        src = (ROOT / "core" / "team_checkout_service.py").read_text(
            encoding="utf-8")
        i = src.index("    if url:\n        if assigned:")
        seg = src[i:i + 1400]
        self.assertIn('"using"', seg)
        self.assertNotIn('mark_promo_code(assigned["id"], "used"', seg,
                         "提链阶段不能直接标 used，支付还没发生")

    def test_claim_starts_as_using(self):
        from core import team_checkout_service as svc
        import inspect
        src = inspect.getsource(svc.claim_next_promo_code)
        self.assertIn("'using'", src)

    def test_payment_promotes_to_used(self):
        """支付成功后才升 used。"""
        import inspect
        from core import vcc_pay_session as ps
        src = inspect.getsource(ps._record_promo)
        self.assertIn("record_used_promo_code", src)

    def test_promo_recorded_only_after_submit(self):
        """_record_promo 必须在 ST_DONE 之后调用，不能填完表就记。"""
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        done = src.index("state=ST_DONE")
        rec = src.index("_record_promo(sess)")
        self.assertLess(done, rec, "必须先到 done 状态才登记优惠码")

    def test_record_promotes_using(self):
        """record_used_promo_code 要能把 using 升成 used。"""
        import inspect
        from core import team_checkout_service as svc
        src = inspect.getsource(svc.record_used_promo_code)
        # 只对已是 used 的短路，其余（含 using）都会被更新
        self.assertIn('== "used"', src)
        self.assertIn("UPDATE promo_codes SET status", src)


class ReclaimTest(unittest.TestCase):
    """卡在 using 的码要能退回。"""

    def test_function_exists(self):
        from core import team_checkout_service as svc
        self.assertTrue(hasattr(svc, "reclaim_stale_promo_codes"))

    def test_clears_account_binding(self):
        """退回时要清掉账号归属，否则下次分派会带着旧账号信息。"""
        import inspect
        from core import team_checkout_service as svc
        src = inspect.getsource(svc.reclaim_stale_promo_codes)
        self.assertIn("account_id=NULL", src)
        self.assertIn("account_email=NULL", src)

    def test_only_touches_using(self):
        """不能动 used/expired/unused。"""
        import inspect
        from core import team_checkout_service as svc
        src = inspect.getsource(svc.reclaim_stale_promo_codes)
        self.assertIn("status = 'using'", src)

    def test_respects_grace_period(self):
        """刚领走的可能正在支付，不能立刻退回。"""
        import inspect
        from core import team_checkout_service as svc
        src = inspect.getsource(svc.reclaim_stale_promo_codes)
        self.assertIn("older_than_minutes", src)
        self.assertIn("cutoff", src)

    def test_default_grace_is_sane(self):
        import inspect
        from core import team_checkout_service as svc
        sig = inspect.signature(svc.reclaim_stale_promo_codes)
        d = sig.parameters["older_than_minutes"].default
        self.assertGreaterEqual(d, 10.0, "宽限期太短会把正在用的码抢回去")

    def test_records_reason(self):
        """退回要留原因，便于事后追查。"""
        import inspect
        from core import team_checkout_service as svc
        src = inspect.getsource(svc.reclaim_stale_promo_codes)
        self.assertIn("last_error", src)


class FailurePathTest(unittest.TestCase):
    """提链失败时的处理不能回退。"""

    def test_failed_link_releases_code(self):
        src = (ROOT / "core" / "team_checkout_service.py").read_text(
            encoding="utf-8")
        self.assertIn('mark_promo_code(assigned["id"], "expired" if promo_issue else "unused"',
                      src)

    def test_explicit_promo_still_recorded(self):
        """显式传码在支付成功后仍要登记（#83 那个坑）。"""
        src = (ROOT / "core" / "team_checkout_service.py").read_text(
            encoding="utf-8")
        self.assertIn("record_used_promo_code(promo", src)


if __name__ == "__main__":
    unittest.main()
