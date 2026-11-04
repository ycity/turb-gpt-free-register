# -*- coding: utf-8 -*-
"""代理池列表默认按名称排序。

只排展示结果，不排 PROXY_POOL_STATE.items——后者的顺序就是 .env 里的写入
顺序，保存时按它整表回写，排原数组会把展示顺序写进配置文件。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HTML = (Path(__file__).resolve().parents[1]
        / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = HTML.index(f"function {name}()")
    j = HTML.index("\nfunction ", i + 10)
    return HTML[i:j]


class SortWiringTest(unittest.TestCase):
    def test_sort_applied_in_filtered_items(self):
        seg = _fn("ppFilteredItems")
        self.assertIn(".sort(", seg)
        self.assertIn("localeCompare", seg)

    def test_sorts_copy_not_state_items(self):
        """st.items 必须保持 .env 顺序，否则保存会改写配置里的排列。"""
        seg = _fn("ppFilteredItems")
        self.assertNotIn("st.items.sort", seg)
        self.assertRegex(seg, r"const rows = st\.items\.filter")
        self.assertRegex(seg, r"return rows\.sort")

    def test_numeric_collation_enabled(self):
        """Frontier-2 要排在 Frontier-10 前面，不能用字典序。"""
        seg = _fn("ppFilteredItems")
        self.assertIn("numeric: true", seg)

    def test_case_insensitive(self):
        seg = _fn("ppFilteredItems")
        self.assertIn("sensitivity: 'base'", seg)

    def test_unnamed_sink_to_bottom(self):
        seg = _fn("ppFilteredItems")
        self.assertIn("!an !== !bn", seg)

    def test_save_still_uses_state_order(self):
        """保存走 PROXY_POOL_STATE.items，与展示排序无关。"""
        self.assertIn("PROXY_POOL_STATE.items.map(item => ({ raw: item.raw }))", HTML)

    def test_selection_keyed_by_identity_not_index(self):
        """选中态若按下标记录，排序后会勾错行。"""
        # 全选处理：按 identity 增删
        i = HTML.index("[data-pp-select-all]")
        seg = HTML[i:i + 500]
        self.assertIn("ppIdentity(item)", seg)
        # 复选框回显：同样按 identity 比对
        j = HTML.index("data-pp-select-all]')")
        seg2 = HTML[j:j + 300]
        self.assertIn("rows.map(ppIdentity)", seg2)


if __name__ == "__main__":
    unittest.main()
