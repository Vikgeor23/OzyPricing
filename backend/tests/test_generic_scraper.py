"""Tests for the generic product scraper fallback."""

import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from app.scrapers.registry import get_scraper_for_domain
from app.scrapers.sites.generic import GenericProductScraper


class GenericProductScraperTests(unittest.TestCase):
    def test_json_ld_product_extracts_core_fields(self) -> None:
        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "ACME Kettle 2000",
            "image": "https://example.com/kettle.jpg",
            "gtin13": "3800000000003",
            "mpn": "KTL-2000",
            "brand": {"@type": "Brand", "name": "ACME"},
            "offers": {
              "@type": "Offer",
              "price": "129.99",
              "priceCurrency": "BGN",
              "availability": "https://schema.org/InStock"
            }
          }
          </script>
        </head><body></body></html>
        """
        result = GenericProductScraper("https://shop.example/p/kettle")._parse_html_to_result(
            html,
            extra_raw={"fetch_layer": "http"},
            captured_at=datetime.now(timezone.utc),
        )

        self.assertEqual(result.title, "ACME Kettle 2000")
        self.assertEqual(result.price, Decimal("129.99"))
        self.assertEqual(result.currency, "BGN")
        self.assertEqual(result.availability, "in_stock")
        self.assertEqual(result.image_url, "https://example.com/kettle.jpg")
        self.assertEqual(result.raw_data["confidence"], "high")
        self.assertEqual(result.raw_data["product_identifiers"]["ean"], "3800000000003")
        self.assertEqual(result.raw_data["product_identifiers"]["manufacturer_code"], "KTL-2000")
        self.assertEqual(result.raw_data["product_identifiers"]["brand"], "ACME")

    def test_meta_and_selector_price_extract(self) -> None:
        html = """
        <html>
          <head>
            <meta property="og:title" content="Wireless Mouse">
            <meta property="og:image" content="/mouse.jpg">
          </head>
          <body>
            <div class="product">
              <span class="old-price">79,90 лв.</span>
              <span class="price">59,90 лв.</span>
              <span>В наличност</span>
            </div>
          </body>
        </html>
        """
        result = GenericProductScraper("https://shop.example/products/mouse")._parse_html_to_result(
            html,
            extra_raw={"fetch_layer": "http"},
            captured_at=datetime.now(timezone.utc),
        )

        self.assertEqual(result.title, "Wireless Mouse")
        self.assertEqual(result.price, Decimal("59.90"))
        self.assertEqual(result.old_price, Decimal("79.90"))
        self.assertEqual(result.promo_price, Decimal("59.90"))
        self.assertEqual(result.currency, "BGN")
        self.assertEqual(result.availability, "in_stock")
        self.assertEqual(result.image_url, "https://shop.example/mouse.jpg")
        self.assertEqual(result.raw_data["confidence"], "medium")

    def test_euro_decimal_price_with_space_before_currency(self) -> None:
        html = """
        <html>
          <head><title>LEGO Friends Calendar</title></head>
          <body>
            <h1>LEGO Friends Calendar</h1>
            <span class="price">25.51 €</span>
          </body>
        </html>
        """
        result = GenericProductScraper("https://shop.example/lego-42668")._parse_html_to_result(
            html,
            extra_raw={"fetch_layer": "http"},
            captured_at=datetime.now(timezone.utc),
        )

        self.assertEqual(result.price, Decimal("25.51"))
        self.assertEqual(result.currency, "EUR")

    def test_extracts_product_code_but_not_description_or_attributes(self) -> None:
        # The product code (SKU) is still extracted, but descriptions and
        # free-form attributes are intentionally no longer collected — only the
        # variant size/color drive matching.
        html = """
        <html>
          <head><meta name="description" content="Brick-built family toy set."></head>
          <body>
            <h1>LEGO Bluey 11217</h1>
            <div>№ 0011217</div>
            <div class="product-meta">
              <span>Марка: LEGO</span>
              <span>Серия: Bluey</span>
            </div>
            <span class="price">49.99 лв.</span>
          </body>
        </html>
        """
        result = GenericProductScraper("https://shop.example/lego-bluey-11217")._parse_html_to_result(
            html,
            extra_raw={"fetch_layer": "http"},
            captured_at=datetime.now(timezone.utc),
        )

        self.assertEqual(result.raw_data["product_identifiers"]["sku"], "0011217")
        self.assertNotIn("description", result.raw_data.get("raw_identifiers") or {})
        self.assertNotIn("марка", result.raw_data.get("specs_json") or {})

    def test_registry_unknown_domain_uses_generic_scraper(self) -> None:
        scraper = get_scraper_for_domain("example.com", "https://example.com/p/1")
        self.assertIsInstance(scraper, GenericProductScraper)


def _notino_variant_json(*, web_id, order, mfr, ean, size, url, value, regular) -> str:
    return (
        '{"__typename":"CatalogVariant","webId":"%s","name":"Atoderm Gel",'
        '"orderCode":"%s","productCode":"%s","eanCode":"%s","additionalInfo":"%s",'
        '"url":"%s","parameters":{"__typename":"Parameters","amount":%s,"unit":"мл."},'
        '"price":{"__typename":"Price","value":%s,"currency":"EUR","tax":20},'
        '"originalPrice":{"__typename":"OriginalPrice","value":%s,"currency":"EUR","type":"Recommended"}}'
    ) % (web_id, order, mfr, ean, size, url, size.split()[0], value, regular)


_NOTINO_VARIANTS_HTML = (
    "<html><body><script>window.__APOLLO_STATE__ = {" + ",".join(
        _notino_variant_json(
            web_id=w, order=o, mfr=m, ean=e, size=s, url=u, value=v, regular=r,
        )
        for (w, o, m, e, s, u, v, r) in [
            ("111", "BIR0755", "MFR-1000", "3701129811542", "1000 мл.",
             "/bioderma/atoderm/p-111/", "18.8", "23.7"),
            ("222", "BIR0561", "MFR-0500", "3701129811573", "500 мл.",
             "/bioderma/atoderm/p-222/", "14.8", "17.8"),
            ("333", "BIR0563", "MFR-0200", "3701129811580", "200 мл.",
             "/bioderma/atoderm/p-333/", "10.6", "12.4"),
        ]
    ) + "};</script></body></html>"
)


class NotinoVariantExpansionTests(unittest.TestCase):
    def test_extract_variants_parses_every_size(self) -> None:
        from app.scrapers.sites.generic import extract_variants

        variants = extract_variants(
            _NOTINO_VARIANTS_HTML,
            "https://www.notino.bg/bioderma/atoderm/p-111/",
        )
        self.assertEqual(len(variants), 3)
        by_size = {v["size"]: v for v in variants}
        self.assertEqual(set(by_size), {"1000ML", "500ML", "200ML"})
        v500 = by_size["500ML"]
        self.assertEqual(v500["price"], Decimal("14.8"))
        self.assertEqual(v500["regular"], Decimal("17.8"))
        self.assertEqual(v500["ean"], "3701129811573")
        self.assertEqual(v500["shop_code"], "BIR0561")
        self.assertEqual(v500["manufacturer_code"], "MFR-0500")
        self.assertTrue(v500["url"].endswith("/p-222/"))

    def test_variant_color_extracted_from_additional_info(self) -> None:
        # Foundations pack shade + size in one field, e.g. "цвят F2 23 мл." —
        # the color must come out as "F2" and the size as "23ML".
        from app.scrapers.sites.generic import _variant_color, _variant_size

        node = {"additionalInfo": "цвят F2 23\xa0мл.", "colors": ["#E0C09F"]}
        self.assertEqual(_variant_color(node), "F2")
        self.assertEqual(_variant_size(node), "23ML")
        # A size-only variant (e.g. a shower gel) has no color.
        self.assertIsNone(_variant_color({"additionalInfo": "1000 мл."}))
        # No label at all falls back to the swatch hex.
        self.assertEqual(_variant_color({"additionalInfo": "", "colors": ["#ABCDEF"]}), "#ABCDEF")

    def test_non_notino_url_yields_no_variants(self) -> None:
        from app.scrapers.sites.generic import extract_variants

        self.assertEqual(extract_variants(_NOTINO_VARIANTS_HTML, "https://shop.example/p/1"), [])

    def test_scraped_row_adopts_its_own_variant_identity(self) -> None:
        # Scraping the 500ml p-id must yield the 500ml price + identity, not the
        # cheapest sibling's (the historical collapse bug).
        result = GenericProductScraper(
            "https://www.notino.bg/bioderma/atoderm/p-222/",
            preferred_currency="EUR",
        )._parse_html_to_result(
            _NOTINO_VARIANTS_HTML,
            extra_raw={"fetch_layer": "http"},
            captured_at=datetime.now(timezone.utc),
        )
        self.assertEqual(result.price, Decimal("17.8"))
        self.assertEqual(result.promo_price, Decimal("14.8"))
        self.assertEqual(result.raw_data["product_identifiers"]["ean"], "3701129811573")
        self.assertEqual(result.raw_data["specs_json"]["size"], "500ML")
        # The two other sizes are carried for sibling materialisation.
        self.assertEqual(len(result.variants or []), 2)

    def test_with_status_preserves_variants(self) -> None:
        # Regression: the real scrape path returns via _with_status, which must
        # carry the variants through — otherwise siblings never reach persist.
        scraper = GenericProductScraper(
            "https://www.notino.bg/bioderma/atoderm/p-222/",
            preferred_currency="EUR",
        )
        parsed = scraper._parse_html_to_result(
            _NOTINO_VARIANTS_HTML,
            extra_raw={"fetch_layer": "http"},
            captured_at=datetime.now(timezone.utc),
        )
        self.assertEqual(len(parsed.variants or []), 2)
        finished = scraper._with_status(parsed, "success", time.perf_counter())
        self.assertEqual(len(finished.variants or []), 2)
        self.assertEqual(finished.raw_data["scraper_status"], "success")


if __name__ == "__main__":
    unittest.main()
