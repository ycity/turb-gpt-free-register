# -*- coding: utf-8 -*-
"""手动支付的收尾动作必须和自动支付一致。

自动支付成功后会做四件事：登记优惠码、记 paid_at、清长链、延迟查套餐。
手动支付原本缺了 paid_at，而且立刻查套餐（OpenAI 写套餐有延迟，查到的
还是旧的 free），结果就是编号分不出来，要人工补。#69 就是这么补的。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
SESSION = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")


def _manual_body() -> str:
    """取整个 api_pay_manual_done 函数体。

    不用固定长度截断：函数一加代码，被截掉的部分就会让断言假失败
    （既有的 test_manual_pay_panel 就是这么挂的）。
    """
    i = APP.index("def api_pay_manual_done")
    end = APP.find("\n    @app.", i)
    return APP[i:end if end > 0 else len(APP)]


class ParityTest(unittest.TestCase):
    """逐项对齐自动支付的收尾动作。"""

    def test_records_promo(self):
        self.assertIn("record_used_promo_code", _manual_body())

    def test_records_paid_at(self):
        """缺这个编号就分不出来，#69 当初要人工补。"""
        self.assertIn("set_account_paid_at", _manual_body())

    def test_clears_checkout_link(self):
        self.assertIn("clear=True", _manual_body())

    def test_enqueues_plan_check(self):
        self.assertIn("enqueue_account_plan_check", _manual_body())

    def test_auto_path_has_same_four(self):
        """对照组：自动支付这四件事都在，顺序也固定。"""
        i = SESSION.index("state=ST_DONE")
        body = SESSION[i:i + 400]
        for fn in ("_record_promo", "_record_paid_at",
                   "_clear_checkout_link", "_schedule_plan_check"):
            self.assertIn(fn, body)


class DelayedPlanCheckTest(unittest.TestCase):
    def test_plan_check_is_delayed(self):
        """立刻查多半还是旧值，必须和自动支付一样等一会儿。"""
        body = _manual_body()
        self.assertIn("VCC_AUTO_PLAN_CHECK_DELAY", body)
        self.assertIn("time.sleep", body)

    def test_runs_in_background_thread(self):
        """不能让前端干等 30 秒。"""
        body = _manual_body()
        self.assertIn("threading.Thread", body)
        self.assertIn("daemon=True", body)

    def test_paid_at_recorded_before_plan_check(self):
        """查套餐回来要据 paid_at 分配编号，顺序反了就分不出。"""
        body = _manual_body()
        self.assertLess(body.index("set_account_paid_at"),
                        body.index("VCC_AUTO_PLAN_CHECK_DELAY"))

    def test_each_step_failure_is_isolated(self):
        """任何一步失败都不能让整个登记请求挂掉——钱已经付了。"""
        body = _manual_body()
        self.assertGreaterEqual(body.count("except Exception"), 4)

    def test_reports_paid_at_honestly(self):
        """已有 paid_at 时 set_account_paid_at 返回 False，不能谎报"已记录"。"""
        body = _manual_body()
        self.assertIn("if db.set_account_paid_at(account_id):", body)
        self.assertIn("此前已记录", body)


class ButtonTest(unittest.TestCase):
    def test_button_says_payment_done(self):
        """原来叫"登记并查套餐"，看不出是"我付完了"的意思。"""
        self.assertIn("我已完成支付", HTML)

    def test_button_is_primary(self):
        i = HTML.index('id="btnManualPayDone"')
        self.assertIn("jobs-tb-btn--primary", HTML[max(0, i - 200):i])

    def test_confirms_before_submit(self):
        """点错会记下一个不存在的支付，还占掉一个编号。"""
        i = HTML.index("async function _manualPayDone")
        body = HTML[i:i + 1800]
        self.assertIn("confirm(", body)
        self.assertLess(body.index("confirm("), body.index("fetch("))

    def test_explains_what_happens(self):
        i = HTML.index("⑤ 支付成功后点这里")
        body = HTML[i:i + 700]
        for word in ("优惠码", "支付时间", "长链", "编号"):
            self.assertIn(word, body)

    def test_warns_against_clicking_without_paying(self):
        i = HTML.index("⑤ 支付成功后点这里")
        self.assertIn("付款没成功就别点", HTML[i:i + 700])

    def test_button_reset_on_reopen(self):
        """点完变成"已登记"并禁用，重开面板必须复位。"""
        i = HTML.index("$('#manualPayPromo').value = '';")
        body = HTML[i:i + 400]
        self.assertIn("btnManualPayDone", body)
        self.assertIn("disabled = false", body)

    def test_refreshes_after_delay(self):
        """套餐是延迟查的，立刻刷新看不到编号。"""
        i = HTML.index("async function _manualPayDone")
        body = HTML[i:i + 1800]
        self.assertIn("setTimeout(", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
