# -*- coding: utf-8 -*-
"""账号列表排序。

规则：没开通订阅的排前面（id 降序，新注册的先看到），
已开通 team 的排后面（下次账单时间降序，快到期的先看到）。
"""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
DB = (ROOT / "core" / "db.py").read_text(encoding="utf-8")


class OrderByTest(unittest.TestCase):
    def test_defined_once(self):
        """重复定义会让人改错地方。"""
        self.assertEqual(DB.count("\n_ACCOUNTS_ORDER_BY = ("), 1)

    def test_team_sorts_last(self):
        from core.db import _ACCOUNTS_ORDER_BY
        self.assertIn("'team'", _ACCOUNTS_ORDER_BY)
        self.assertIn("THEN 1 ELSE 0 END ASC", _ACCOUNTS_ORDER_BY)

    def test_team_ordered_by_renewal_desc(self):
        from core.db import _ACCOUNTS_ORDER_BY
        self.assertIn("plan_renews_at", _ACCOUNTS_ORDER_BY)
        i = _ACCOUNTS_ORDER_BY.index("plan_renews_at")
        self.assertIn("DESC", _ACCOUNTS_ORDER_BY[i:i + 120])

    def test_falls_back_to_id_desc(self):
        """没开通的没有账单时间，要靠 id 降序。"""
        from core.db import _ACCOUNTS_ORDER_BY
        self.assertTrue(_ACCOUNTS_ORDER_BY.rstrip().endswith("id DESC"))

    def test_no_user_input_in_sql(self):
        """order_by 拼进 SQL，只能用写死的常量。"""
        from core import db
        src = inspect.getsource(db.list_accounts_page)
        self.assertIn("order_by=_ACCOUNTS_ORDER_BY", src)
        self.assertNotIn("order_by=order", src)

    def test_sort_param_documented_as_unsafe(self):
        from core import db
        src = inspect.getsource(db._query_collection_page)
        self.assertIn("不能来自用户输入", src)

    def test_other_collections_unchanged(self):
        """这是通用函数，别的集合必须保持 id DESC。"""
        from core import db
        src = inspect.getsource(db._query_collection_page)
        self.assertIn('order_by or "id DESC"', src)

    def test_status_poll_uses_same_order(self):
        """轮询和列表排序不一致的话，刷新后行会跳。"""
        from core import db
        src = inspect.getsource(db.list_account_plan_check_statuses)
        self.assertIn("order_by=_ACCOUNTS_ORDER_BY", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
