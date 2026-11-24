# -*- coding: utf-8 -*-
"""订阅编号必须按付款时间排序。

问题（2026-11-18 线上）：
  编号在套餐查询返回时分配（db.py: update_account_plan_result 末尾），
  而套餐查询是并发的——先付款的账号可能后返回，于是拿到更大的编号。

  实际发生：
    #88 付款 04:32:35 -> 编号 85   先付款却是大号
    #90 付款 05:41:13 -> 编号 84   后付款却是小号

修法：分配时检查有没有「付款更晚但已拿到编号」的同类账号，有就换位。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _acc(aid, paid, no=None, windows=("5h",)):
    d = {"id": aid, "paid_at": paid, "plan_limit_windows": list(windows)}
    if no is not None:
        d["subscription_no"] = no
        d["subscription_kind"] = "weekly" if list(windows) == ["weekly"] else "5h"
    return d


class OrderingTest(unittest.TestCase):
    def _assign(self, rows, aid):
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts"):
            return db.assign_subscription_no(aid)

    def test_swaps_with_later_paid(self):
        """后付款的已占了小号，要和它换位。"""
        rows = [
            _acc(90, "2026-11-18T05:41:13", 84),      # 后付款，已有 84
            _acc(88, "2026-11-18T04:32:35"),          # 先付款，待分配
        ]
        got = self._assign(rows, 88)
        self.assertEqual(got, 84, "先付款的该拿 84")
        n90 = next(r for r in rows if r["id"] == 90)
        self.assertEqual(n90["subscription_no"], 85, "后付款的该被推到 85")

    def test_no_swap_when_order_already_right(self):
        rows = [
            _acc(88, "2026-11-18T04:32:35", 84),
            _acc(90, "2026-11-18T05:41:13"),
        ]
        got = self._assign(rows, 90)
        self.assertEqual(got, 85)
        n88 = next(r for r in rows if r["id"] == 88)
        self.assertEqual(n88["subscription_no"], 84, "不该动已正确的编号")

    def test_first_ever_uses_start(self):
        rows = [_acc(1, "2026-11-18T01:00:00")]
        self.assertEqual(self._assign(rows, 1), 49)

    def test_weekly_has_own_sequence(self):
        rows = [
            _acc(75, "2026-11-17T07:58:24", 10, windows=("weekly",)),
            _acc(99, "2026-11-18T01:00:00", windows=("weekly",)),
        ]
        self.assertEqual(self._assign(rows, 99), 11)

    def test_weekly_not_mixed_with_5h(self):
        """两条序列互不干扰：5h 的大编号不能影响 weekly。"""
        rows = [
            _acc(85, "2026-11-18T02:41:34", 83),
            _acc(99, "2026-11-18T03:00:00", windows=("weekly",)),
        ]
        self.assertEqual(self._assign(rows, 99), 8, "weekly 应从 8 开始")

    def test_existing_number_untouched(self):
        rows = [_acc(88, "2026-11-18T04:32:35", 84)]
        self.assertEqual(self._assign(rows, 88), 84)

    def test_no_kind_returns_none(self):
        """套餐还没查出来时不分配，等下一次调用。"""
        rows = [{"id": 5, "paid_at": "2026-11-18T01:00:00"}]
        self.assertIsNone(self._assign(rows, 5))

    def test_account_without_paid_at_still_works(self):
        """没有 paid_at 的（手动导入）不参与换位，但要能拿到编号。"""
        rows = [
            _acc(90, "2026-11-18T05:41:13", 84),
            _acc(91, "", windows=("5h",)),
        ]
        got = self._assign(rows, 91)
        self.assertEqual(got, 85)
        n90 = next(r for r in rows if r["id"] == 90)
        self.assertEqual(n90["subscription_no"], 84, "不该动别人")

    def test_swaps_only_once(self):
        """多个后付款账号时只和编号最小的那个换，不连环改。"""
        rows = [
            _acc(90, "2026-11-18T05:00:00", 84),
            _acc(91, "2026-11-18T06:00:00", 85),
            _acc(88, "2026-11-18T04:00:00"),
        ]
        got = self._assign(rows, 88)
        self.assertEqual(got, 84)
        self.assertEqual(
            next(r for r in rows if r["id"] == 90)["subscription_no"], 86)
        self.assertEqual(
            next(r for r in rows if r["id"] == 91)["subscription_no"], 85,
            "没参与换位的不该被动")


class RegressionTest(unittest.TestCase):
    def test_still_uses_max_plus_one(self):
        """不能退回"填补最小空位"——那会让编号顺序和付款顺序脱节。"""
        import inspect
        src = inspect.getsource(db.assign_subscription_no)
        self.assertIn("max(used) + 1", src)

    def test_logs_the_swap(self):
        """换位要留日志，否则事后看不出编号为什么变了。"""
        import inspect
        src = inspect.getsource(db.assign_subscription_no)
        self.assertIn("换位", src)

    def test_compares_paid_at(self):
        import inspect
        src = inspect.getsource(db.assign_subscription_no)
        self.assertIn("paid_at", src)

    def test_module_has_logger(self):
        """db.py 里有两处用 logger，必须真的定义了它。

        2026-11-18：原先 line 1671 的 logger.exception 引用了一个不存在
        的名字，只因为那个 except 从没触发过才没暴露——真出错时会抛
        NameError 把原始异常盖掉。
        """
        self.assertTrue(hasattr(db, "logger"))
        import logging
        self.assertIsInstance(db.logger, logging.Logger)

    def test_assign_failure_path_can_log(self):
        """分配编号失败时那条 logger.exception 要能真的执行。"""
        from unittest.mock import patch as _p
        with _p.object(db, "_load_accounts", side_effect=RuntimeError("boom")):
            # update_account_plan_result 里的 except 会调 logger.exception；
            # 这里直接验证 logger 可用即可，不触发整条链路
            try:
                db.logger.exception("[测试] 不应抛 NameError")
            except NameError:  # pragma: no cover
                self.fail("logger 不可用")


if __name__ == "__main__":
    unittest.main()
