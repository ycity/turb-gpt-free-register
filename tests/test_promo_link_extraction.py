import unittest

from core.team_checkout_service import extract_clean_promo_code as extract


class PromoLinkExtractionTests(unittest.TestCase):
    def test_share_link_uses_last_path_segment(self):
        # 用户报告：这类链接原先全被提取成 "https"，导致批量导入只进一条。
        self.assertEqual(extract("https://chatgpt.com/p/JLQAF3GJ3VZWSGDK"),
                         "JLQAF3GJ3VZWSGDK")
        self.assertEqual(extract("https://chatgpt.com/p/JRBY9HQC7UXBQ5AR"),
                         "JRBY9HQC7UXBQ5AR")

    def test_two_share_links_yield_distinct_codes(self):
        a = extract("https://chatgpt.com/p/JLQAF3GJ3VZWSGDK")
        b = extract("https://chatgpt.com/p/JRBY9HQC7UXBQ5AR")
        self.assertNotEqual(a, b)
        self.assertNotIn("https", (a, b))

    def test_trailing_slash_and_query_are_ignored(self):
        self.assertEqual(extract("https://chatgpt.com/p/ABCD1234/"), "ABCD1234")
        self.assertEqual(extract("https://chatgpt.com/p/ABCD1234?utm_source=x"),
                         "ABCD1234")
        self.assertEqual(extract("https://chatgpt.com/p/ABCD1234#frag"), "ABCD1234")

    def test_scheme_relative_and_bare_domain(self):
        self.assertEqual(extract("http://chatgpt.com/p/lowercase123"), "lowercase123")
        self.assertEqual(extract("chatgpt.com/p/ABCD1234"), "ABCD1234")

    def test_query_parameter_links_still_win(self):
        self.assertEqual(extract("https://chatgpt.com/promo?promo=ABCD1234"), "ABCD1234")
        self.assertEqual(extract("https://x.test/a?coupon=XYZ789"), "XYZ789")
        self.assertEqual(extract("https://x.test/p/IGNORED?promo=WINS"), "WINS")

    def test_plain_codes_unchanged(self):
        self.assertEqual(extract("JLQAF3GJ3VZWSGDK"), "JLQAF3GJ3VZWSGDK")
        self.assertEqual(extract("  ABCD1234  "), "ABCD1234")

    def test_empty_input(self):
        self.assertEqual(extract(""), "")
        self.assertEqual(extract("   "), "")
        self.assertEqual(extract(None), "")


if __name__ == "__main__":
    unittest.main()
