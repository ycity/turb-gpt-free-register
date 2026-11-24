# -*- coding: utf-8 -*-
"""界面时间统一按新加坡时间（UTC+8）展示。

问题：注册任务列表的开始/完成时间用的是 formatDateTime，而它取值走
d.getFullYear()/getHours()——那是**浏览器本地**时区。后端 created_at /
started_at 这类是容器 datetime.now() 产出的裸 UTC（形如
2026-11-17T04:17:20，没有 Z），直接丢给 new Date() 会被当成本地时间解析。
结果是：显示的其实是 UTC，却按本地时间标注，整整差 8 小时。

账号表那边早就用 _fmtSgt 修好了，这次把注册任务、优惠码、邮箱池也统一。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")


def _fn(name):
    """抠出一个 JS 函数体。"""
    i = HTML.index("function %s(" % name)
    depth, j, started = 0, i, False
    while j < len(HTML):
        if HTML[j] == "{":
            depth += 1
            started = True
        elif HTML[j] == "}":
            depth -= 1
            if started and depth == 0:
                return HTML[i:j + 1]
        j += 1
    raise AssertionError("找不到函数 %s" % name)


def _code(name):
    """函数体去掉 // 注释，只留可执行代码。

    注释里会提到"原本用 d.getFullYear()"这类字样，直接搜整段会把
    注释当成代码命中，测试就白写了。
    """
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in _fn(name).splitlines())


class FormatDateTimeTest(unittest.TestCase):
    def test_does_not_use_browser_local_getters(self):
        """不能再用本地时区取值，否则换台机器看到的时间就不一样。"""
        src = _code("formatDateTime")
        for bad in ("d.getFullYear()", "d.getMonth()", "d.getDate()",
                    "d.getHours()", "d.getMinutes()", "d.getSeconds()"):
            self.assertNotIn(bad, src, "仍在用浏览器本地时区取值：%s" % bad)

    def test_uses_utc_getters(self):
        """改用 UTC 取值 + 固定偏移，结果与浏览器所在时区无关。"""
        src = _code("formatDateTime")
        self.assertIn("getUTCFullYear()", src)
        self.assertIn("getUTCHours()", src)

    def test_applies_eight_hour_offset(self):
        src = _code("formatDateTime")
        self.assertIn("8 * 3600 * 1000", src)

    def test_reuses_naive_utc_parser(self):
        """裸时间戳要按 UTC 解析，这个判断已经在 _toSgt 里了，别重写一遍。"""
        src = _code("formatDateTime")
        self.assertIn("_toSgt", src)

    def test_keeps_seconds(self):
        """注册任务看的是耗时，秒不能丢。"""
        src = _code("formatDateTime")
        self.assertIn("getUTCSeconds()", src)

    def test_still_handles_blank_and_bad_input(self):
        src = _code("formatDateTime")
        self.assertIn("return '-'", src)
        self.assertIn("Number.isNaN", src)


class NaiveUtcParserTest(unittest.TestCase):
    def test_treats_missing_tz_as_utc(self):
        src = _code("_toSgt")
        self.assertIn("hasTz", src)
        self.assertIn("+ 'Z'", src)

    def test_respects_explicit_tz(self):
        """带时区的（renews_at 形如 ...+00:00）不能再被强行当成 UTC 拼 Z。"""
        src = _code("_toSgt")
        self.assertIn("hasTz", src)
        self.assertIn("Z|[+-]", src)


class CallSiteTest(unittest.TestCase):
    """所有展示后端时间的地方都要走统一格式化，不能直接裸显示。"""

    def test_job_list_uses_formatter(self):
        self.assertIn("formatDateTime(j.started_at)", HTML)
        self.assertIn("formatDateTime(j.completed_at)", HTML)

    def test_promo_list_uses_formatter(self):
        """优惠码列表原来是 slice(0,19) 裸显示 UTC。"""
        i = HTML.index("item.account_email || (item.account_id")
        seg = HTML[i:i + 400]
        self.assertIn("formatDateTime(item.created_at)", seg)
        self.assertNotIn("item.created_at || '').slice(0, 19)", seg)

    def test_email_pool_uses_formatter(self):
        """邮箱池的导入/使用时间原来也是裸显示。"""
        i = HTML.index('<td class="col-source">${esc(poolLabel(r.source))}')
        seg = HTML[i:i + 700]
        self.assertIn("formatDateTime(r.imported_at || r.created_at)", seg)
        self.assertIn("formatDateTime(r.used_at)", seg)

    def test_raw_timestamp_kept_in_tooltip(self):
        """原始值要留在 title 里，排查时还得看 UTC 原文。"""
        i = HTML.index('<td class="col-source">${esc(poolLabel(r.source))}')
        seg = HTML[i:i + 700]
        self.assertIn("title=\"${esc(r.imported_at || r.created_at || '-')}\"", seg)


class AccountTableParityTest(unittest.TestCase):
    """账号表那套已经是对的，别被这次改动带歪。"""

    def test_account_table_still_uses_sgt(self):
        self.assertIn("_fmtSgt(r.created_at)", HTML)

    def test_both_formatters_share_offset(self):
        """两个格式化函数必须同一个偏移，否则两张表对不上。"""
        for name in ("_fmtSgt", "formatDateTime"):
            self.assertIn("8 * 3600 * 1000", _code(name),
                          "%s 的时区偏移不一致" % name)


if __name__ == "__main__":
    unittest.main()
