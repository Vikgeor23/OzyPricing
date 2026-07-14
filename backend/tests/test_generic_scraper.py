"""Tests for the generic product scraper fallback."""

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
            "gtin13": "3800000000000",
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
        self.assertEqual(result.raw_data["product_identifiers"]["ean"], "3800000000000")
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

    def test_extracts_code_description_and_attributes(self) -> None:
        html = """
        <html>
          <head><meta name="description" content="Brick-built family toy set."></head>
          <body>
            <h1>LEGO Bluey 11217</h1>
            <div>№ 0011217</div>
            <div class="product-meta">
              <span>Категория: Детски играчки</span>
              <span>Вид: Конструктори</span>
              <span>Марка: LEGO</span>
              <span>Серия: Bluey</span>
              <span>Тема: Блуи</span>
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
        self.assertEqual(result.raw_data["raw_identifiers"]["description"], "Brick-built family toy set.")
        self.assertEqual(result.raw_data["specs_json"]["марка"], "LEGO")
        self.assertEqual(result.raw_data["specs_json"]["серия"], "Bluey")
        self.assertEqual(result.raw_data["specs_json"]["тема"], "Блуи")

    def test_unescapes_html_description(self) -> None:
        html = """
        <html>
          <head>
            <meta name="description" content="&lt;p&gt;Fresh cream &lt;strong&gt;for face&lt;/strong&gt;.&lt;/p&gt;">
          </head>
          <body>
            <h1>Face Cream</h1>
            <span class="price">19.99 лв.</span>
          </body>
        </html>
        """
        result = GenericProductScraper("https://shop.example/face-cream")._parse_html_to_result(
            html,
            extra_raw={"fetch_layer": "http"},
            captured_at=datetime.now(timezone.utc),
        )

        self.assertEqual(result.raw_data["raw_identifiers"]["description"], "Fresh cream for face.")

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


if __name__ == "__main__":
    unittest.main()
