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
    """截取某个函数的源码片段（函数可带参数）。"""
    import re
    m = re.search(r"function " + re.escape(name) + r"\s*\(", HTML)
    assert m, f"找不到 function {name}"
    i = m.start()
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


class RegionGroupingTest(unittest.TestCase):
    """先按地区分组，组内再按名称。"""

    @classmethod
    def setUpClass(cls):
        cls.seg = _fn("ppFilteredItems")

    def test_region_compared_before_name(self):
        seg = self.seg
        region_at = seg.index("ppRegionKey")
        name_at = seg.index("String(a.name")
        self.assertLess(region_at, name_at, "地区必须先比，否则分不了组")

    def test_region_key_only_from_test_result(self):
        """地区只能来自测试结果，没测过的拿不到。"""
        seg = _fn("ppRegionKey")
        self.assertIn("test", seg)
        self.assertIn("country", seg)

    def test_unknown_region_sinks_to_bottom(self):
        self.assertIn("if (!ar !== !br) return ar ? -1 : 1;", self.seg)

    def test_group_heads_rendered(self):
        seg = _fn("ppRowsWithRegionHeads")
        self.assertIn("pp-region-head", seg)
        self.assertIn("未知地区", seg)

    def test_body_uses_group_renderer(self):
        seg = _fn("renderProxyPoolBody")
        self.assertIn("ppRowsWithRegionHeads(rows)", seg)
        self.assertNotIn("rows.map(ppRowHtml)", seg,
                         "应改走带分组标题的渲染")

    def test_head_row_styled(self):
        self.assertIn("tr.pp-region-head td", HTML)

    def test_head_row_spans_table(self):
        """标题行必须跨满列数，否则会把表格挤歪。"""
        seg = _fn("ppRowsWithRegionHeads")
        self.assertIn('colspan="7"', seg)


if __name__ == "__main__":
    unittest.main()
