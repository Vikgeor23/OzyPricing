"""Tests for Technopolis full-domain product URL discovery helpers."""

import unittest

from app.scrapers.sites.technopolis_full_discovery import (
    _ProductUrlRegistry,
    _product_candidates_from_locs,
    parse_sitemap_locs,
    resolve_sitemap_loc,
)
from app.scrapers.sites.technopolis_urls import (
    is_technopolis_product_url,
    normalize_technopolis_product_url,
    prefer_technopolis_product_url,
    product_url_dedupe_key,
    technopolis_product_code,
)

SAMPLE_BG = (
    "https://www.technopolis.bg/bg/Smartfoni-i-mobilni-telefoni/"
    "Smartfon-GSM--APPLE-IPHONE-16-BLACK/p/505144"
)
SAMPLE_EN = (
    "https://www.technopolis.bg/en/Smartphones-and-mobile-phones/"
    "Smartphone-GSM--APPLE-IPHONE-16-BLACK/p/505144"
)


class TechnopolisFullDiscoveryTests(unittest.TestCase):
    def test_is_technopolis_product_url_bg_and_en(self) -> None:
        self.assertTrue(is_technopolis_product_url(SAMPLE_BG))
        self.assertTrue(is_technopolis_product_url(SAMPLE_EN))
        self.assertTrue(is_technopolis_product_url(SAMPLE_BG + "?utm_source=newsletter"))
        self.assertFalse(is_technopolis_product_url("https://www.technopolis.bg/bg/telefoni/c/1/"))
        self.assertFalse(is_technopolis_product_url("https://example.com/bg/foo/p/123"))

    def test_normalize_strips_tracking_and_fragment(self) -> None:
        raw = SAMPLE_BG + "?utm_source=x&fbclid=abc#reviews"
        norm = normalize_technopolis_product_url(raw)
        assert norm is not None
        self.assertNotIn("utm_source", norm)
        self.assertNotIn("fbclid", norm)
        self.assertNotIn("#", norm)
        self.assertTrue(norm.endswith("/p/505144"))

    def test_prefer_bg_over_en_for_same_code(self) -> None:
        self.assertEqual(prefer_technopolis_product_url(SAMPLE_EN, SAMPLE_BG), SAMPLE_BG)
        self.assertEqual(prefer_technopolis_product_url(SAMPLE_BG, SAMPLE_EN), SAMPLE_BG)

    def test_registry_prefers_bg_when_en_added_first(self) -> None:
        reg = _ProductUrlRegistry(max_products=100)
        reg.register(SAMPLE_EN)
        reg.register(SAMPLE_BG)
        self.assertEqual(len(reg.products), 1)
        self.assertIn(SAMPLE_BG, reg.products)
        self.assertNotIn(SAMPLE_EN, reg.products)

    def test_registry_skips_en_when_bg_exists(self) -> None:
        reg = _ProductUrlRegistry(max_products=100)
        reg.register(SAMPLE_BG)
        reg.register(SAMPLE_EN)
        self.assertEqual(len(reg.products), 1)
        self.assertIn(SAMPLE_BG, reg.products)

    def test_dedupe_by_product_code(self) -> None:
        a = normalize_technopolis_product_url(SAMPLE_BG)
        b = normalize_technopolis_product_url(SAMPLE_BG + "?utm_campaign=1")
        assert a and b
        self.assertEqual(product_url_dedupe_key(a), product_url_dedupe_key(b))
        self.assertEqual(technopolis_product_code(a), "505144")

    def test_parse_sitemap_urlset_with_namespace(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.technopolis.bg/bg/cat/item/p/505144</loc></url>
          <url><loc>https://www.technopolis.bg/bg/telefoni/</loc></url>
        </urlset>"""
        pages, nested = parse_sitemap_locs(xml)
        self.assertEqual(nested, [])
        self.assertEqual(len(pages), 2)
        self.assertIn("https://www.technopolis.bg/bg/cat/item/p/505144", pages)
        candidates = _product_candidates_from_locs(pages)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].endswith("/p/505144"))

    def test_parse_sitemap_index_with_product_bg_files(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>/sitemapurl/Product-bg-EUR-0.xml</loc></sitemap>
          <sitemap><loc>/sitemapurl/Product-bg-EUR-1.xml</loc></sitemap>
          <sitemap><loc>/sitemapurl/Product-en-EUR-0.xml</loc></sitemap>
        </sitemapindex>"""
        pages, nested = parse_sitemap_locs(xml)
        self.assertEqual(pages, [])
        self.assertEqual(len(nested), 3)
        self.assertIn("/sitemapurl/Product-bg-EUR-0.xml", nested)

    def test_resolve_relative_product_sitemap_url(self) -> None:
        resolved = resolve_sitemap_loc(
            "https://www.technopolis.bg/sitemap.xml",
            "/sitemapurl/Product-bg-EUR-0.xml",
        )
        self.assertEqual(resolved, "https://www.technopolis.bg/sitemapurl/Product-bg-EUR-0.xml")

    def test_product_sitemap_urlset_en_and_bg(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.technopolis.bg/en/cat/item/p/505144</loc></url>
          <url><loc>https://www.technopolis.bg/bg/cat/item/p/505144</loc></url>
        </urlset>"""
        pages, _nested = parse_sitemap_locs(xml)
        reg = _ProductUrlRegistry(max_products=100)
        for loc in pages:
            reg.register(loc)
        self.assertEqual(len(reg.products), 1)
        self.assertTrue(any(u.startswith("https://www.technopolis.bg/bg/") for u in reg.products))


if __name__ == "__main__":
    unittest.main()
