"""Tests for Technopolis category link filtering."""

import unittest

from app.scrapers.sites.technopolis_categories import (
    CategoryNode,
    filter_category_nodes,
    is_category_candidate_url,
    is_excluded_category_name,
    normalize_category_name,
)


class TechnopolisCategoryFilterTests(unittest.TestCase):
    def test_normalize_category_name(self) -> None:
        self.assertEqual(normalize_category_name("  Телефони   "), "Телефони")

    def test_excluded_names(self) -> None:
        self.assertTrue(is_excluded_category_name("Top5 List"))
        self.assertTrue(is_excluded_category_name("Promotions"))
        self.assertTrue(is_excluded_category_name("Weekly Offers"))
        self.assertTrue(is_excluded_category_name("Zero Leasing"))
        self.assertTrue(is_excluded_category_name("P1101"))
        self.assertTrue(is_excluded_category_name(""))
        self.assertFalse(is_excluded_category_name("Смартфони"))

    def test_category_url_with_c_segment(self) -> None:
        url = "https://www.technopolis.bg/bg/telefoni/c/123/"
        self.assertTrue(is_category_candidate_url(url))

    def test_excluded_promo_url(self) -> None:
        url = "https://www.technopolis.bg/bg/promotions/top5-list/"
        self.assertFalse(is_category_candidate_url(url))

    def test_filter_dedupes_labels(self) -> None:
        nodes = [
            CategoryNode("Телефони", "https://www.technopolis.bg/bg/telefoni/c/1/", "/bg/telefoni/c/1/", None, 1, True),
            CategoryNode("Телефони", "https://www.technopolis.bg/bg/telefoni/", "/bg/telefoni/", None, 1, False),
            CategoryNode("P1101", "https://www.technopolis.bg/bg/p1101/", "/bg/p1101/", None, 1, False),
        ]
        out = filter_category_nodes(nodes)
        names = [n.name for n in out]
        self.assertIn("Телефони", names)
        self.assertNotIn("P1101", names)
        self.assertEqual(len([n for n in out if n.name == "Телефони"]), 1)


if __name__ == "__main__":
    unittest.main()
