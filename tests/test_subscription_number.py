# -*- coding: utf-8 -*-
"""订阅编号与支付时间。

需求：支付成功后记录时间，账号列表显示成
    TEAM · 5h限额 (49)
    订阅时间 · 2026.11.05 20:06

编号规则（和用户核对过）：
  - 5h 和周限额各自一条独立序列
  - 5h 从 49 起（id 1~8 那批已删除的账号占了 41~48），周限额从 8 起
  - 手动导入的账号（没有 paid_at）不编号
锚点：#9=49, #15=55, #16=8, #17=56, #62=9
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
DB = (ROOT / "core" / "db.py").read_text(encoding="utf-8")
PAY = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")


class NumberingRuleTest(unittest.TestCase):
    def test_two_independent_sequences(self):
        from core.db import _SUB_NO_START
        self.assertEqual(_SUB_NO_START["5h"], 49)
        self.assertEqual(_SUB_NO_START["weekly"], 8)

    def test_weekly_only_counts_as_weekly(self):
        """['5h','weekly'] 是 5h 账号，只有 ['weekly'] 才算周限额。"""
        from core.db import _limit_kind
        self.assertEqual(_limit_kind({"plan_limit_windows": ["weekly"]}), "weekly")
        self.assertEqual(_limit_kind({"plan_limit_windows": ["5h", "weekly"]}), "5h")

    def test_no_windows_means_no_kind(self):
        """限额还没查出来时不能分配编号，否则会把两条序列排错。"""
        from core.db import _limit_kind
        self.assertEqual(_limit_kind({}), "")
        self.assertEqual(_limit_kind({"plan_limit_windows": []}), "")


class PaymentHookTest(unittest.TestCase):
    def test_records_paid_at_on_success(self):
        self.assertIn("_record_paid_at(sess)", PAY)

    def test_called_only_after_success(self):
        idx = PAY.index("_record_paid_at(sess)")
        window = PAY[max(0, idx - 300):idx]
        self.assertIn("ST_DONE", window)

    def test_does_not_assign_number_at_pay_time(self):
        """付款时还不知道买到的是 5h 还是周限额，编号要等套餐查回来。"""
        idx = PAY.index("def _record_paid_at")
        body = PAY[idx:idx + 900]
        # 注释里会提到这个函数名，所以查的是调用而不是出现
        self.assertNotIn("db.assign_subscription_no(", body)
        self.assertIn("set_account_paid_at", body)

    def test_failure_does_not_break_payment(self):
        idx = PAY.index("def _record_paid_at")
        body = PAY[idx:idx + 900]
        self.assertIn("except Exception", body)


class DbFunctionTest(unittest.TestCase):
    def test_paid_at_not_overwritten(self):
        """重复支付不能把首次订阅时间冲掉。"""
        idx = DB.index("def set_account_paid_at")
        body = DB[idx:idx + 600]
        self.assertIn('row.get("paid_at")', body)

    def test_assign_is_idempotent(self):
        idx = DB.index("def assign_subscription_no")
        body = DB[idx:idx + 1200]
        self.assertIn("existing", body)

    def test_skips_until_limit_known(self):
        idx = DB.index("def assign_subscription_no")
        body = DB[idx:idx + 1200]
        self.assertIn("if not kind:", body)
        self.assertIn("return None", body)

    def test_assigned_after_plan_check(self):
        """限额类型到套餐查完才确定，编号在那之后补发。"""
        self.assertIn("assign_subscription_no(target_id)", DB)

    def test_assign_called_outside_lock(self):
        """assign_subscription_no 自己要拿 _LOCK，在 with 里调会重入死锁。"""
        idx = DB.index("assign_subscription_no(target_id)")
        window = DB[max(0, idx - 500):idx]
        self.assertIn("重入", window)

    def test_only_paid_accounts_numbered(self):
        idx = DB.index("needs_no = ")
        line = DB[idx:DB.index("\n", idx)]
        self.assertIn("paid_at", line)


class ApiExposureTest(unittest.TestCase):
    def test_fields_sent_to_frontend(self):
        for f in ("subscription_no", "paid_at"):
            self.assertIn(f'"{f}"', APP, f"{f} 没下发给前端")

    def test_fields_persisted(self):
        for f in ("subscription_no", "subscription_kind", "paid_at"):
            self.assertIn(f'"{f}"', DB)


class RenderTest(unittest.TestCase):
    def test_number_appended_to_limit(self):
        idx = HTML.index("function _limitSuffix")
        body = HTML[idx:HTML.index("function _teamPlanTag", idx)]
        self.assertIn("r.subscription_no", body)
        self.assertIn("${no}", body)

    def test_no_number_renders_nothing(self):
        """手动导入的账号不该出现空括号。"""
        idx = HTML.index("function _limitSuffix")
        body = HTML[idx:HTML.index("function _teamPlanTag", idx)]
        self.assertIn("r.subscription_no ?", body)

    def test_paid_at_line_exists(self):
        self.assertIn("function _paidAtLine", HTML)
        self.assertIn("订阅时间 ·", HTML)

    def test_paid_at_rendered_in_both_branches(self):
        idx = HTML.index("function _planCell")
        body = HTML[idx:HTML.index("function _planAction")]
        self.assertEqual(body.count("_paidAtLine(r)"), 2,
                         "free 分支和付费分支都要显示订阅时间")

    def test_invalid_date_is_skipped(self):
        idx = HTML.index("function _paidAtLine")
        body = HTML[idx:idx + 900]
        self.assertIn("isNaN", body)

    def test_style_defined(self):
        self.assertIn(".plan-paid-at", HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
