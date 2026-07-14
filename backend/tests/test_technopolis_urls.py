"""Tests for Technopolis product URL parsing."""

import unittest

from app.scrapers.sites.technopolis_breadcrumbs import extract_breadcrumb_categories
from app.scrapers.sites.technopolis_discovery import is_product_detail_url
from app.scrapers.sites.technopolis_urls import (
    is_technopolis_product_detail_url,
    is_technopolis_product_url,
    normalize_technopolis_product_url,
    parse_technopolis_product_url,
    product_url_dedupe_key,
    slug_to_display_name,
)
from bs4 import BeautifulSoup


class TechnopolisUrlTests(unittest.TestCase):
    SAMPLE_URL = (
        "https://www.technopolis.bg/bg/Smartfoni-i-mobilni-telefoni/"
        "Smartfon-GSM--APPLE-IPHONE-16-BLACK/p/505144"
    )

    def test_parse_product_url(self) -> None:
        parsed = parse_technopolis_product_url(self.SAMPLE_URL)
        assert parsed is not None
        self.assertEqual(parsed["url_category_slug"], "Smartfoni-i-mobilni-telefoni")
        self.assertEqual(parsed["url_product_slug"], "Smartfon-GSM--APPLE-IPHONE-16-BLACK")
        self.assertEqual(parsed["technopolis_product_code"], "505144")

    def test_is_product_detail_url(self) -> None:
        self.assertTrue(is_technopolis_product_url(self.SAMPLE_URL))
        self.assertTrue(is_technopolis_product_detail_url(self.SAMPLE_URL))
        self.assertTrue(is_product_detail_url(self.SAMPLE_URL))
        self.assertFalse(is_technopolis_product_url("https://www.technopolis.bg/bg/telefoni/c/1/"))

    def test_normalize_dedupes_tracking_params(self) -> None:
        a = normalize_technopolis_product_url(self.SAMPLE_URL)
        b = normalize_technopolis_product_url(self.SAMPLE_URL + "?utm_source=email")
        assert a and b
        self.assertEqual(a, b)
        self.assertEqual(product_url_dedupe_key(a), product_url_dedupe_key(b))

    def test_slug_to_display_name(self) -> None:
        self.assertEqual(slug_to_display_name("Smartfoni-i-mobilni-telefoni"), "Smartfoni I Mobilni Telefoni")

    def test_extract_breadcrumbs(self) -> None:
        html = """
        <nav class="breadcrumb">
          <a href="/bg/">Начало</a>
          <a href="/bg/tableti/">Смартфони-мобилни телефони и таблети</a>
          <a href="/bg/smartfoni/">Смартфони и мобилни телефони</a>
          <span>Apple iPhone 16 Black</span>
        </nav>
        """
        soup = BeautifulSoup(html, "html.parser")
        crumbs = extract_breadcrumb_categories(
            soup,
            self.SAMPLE_URL,
            product_title="Apple iPhone 16 Black",
        )
        names = [c["name"] for c in crumbs]
        self.assertEqual(
            names,
            ["Смартфони-мобилни телефони и таблети", "Смартфони и мобилни телефони"],
        )
        self.assertTrue(crumbs[0]["url"])


if __name__ == "__main__":
    unittest.main()
