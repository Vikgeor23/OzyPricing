"""Tests for Technopolis specification extraction."""

import unittest

from bs4 import BeautifulSoup

from app.scrapers.sites.technopolis_specs import extract_technopolis_product_specs, normalize_ean

SAMPLE_HTML = """
<html><body>
<h1>Apple iPhone 16 Black 128GB</h1>
<table class="product-characteristics">
  <tr><th>Марка</th><td>Apple</td></tr>
  <tr><th>Баркод</th><td>5901234123457</td></tr>
  <tr><th>Продуктов код</th><td>MQ6K3</td></tr>
  <tr><th>Модел</th><td>iPhone 16</td></tr>
  <tr><th>Памет</th><td>128 GB</td></tr>
  <tr><th>Цвят</th><td>Black</td></tr>
</table>
<script type="application/ld+json">
{"@type":"Product","name":"iPhone 16","gtin13":"5901234123457","brand":{"name":"Apple"},"sku":"MQ6K3"}
</script>
</body></html>
"""


class TechnopolisSpecsTests(unittest.TestCase):
    def test_normalize_ean(self) -> None:
        self.assertEqual(normalize_ean("590 1234 123 457"), "5901234123457")

    def test_extract_from_characteristics_table(self) -> None:
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        data = extract_technopolis_product_specs(soup, url_meta={"product_code": "505144"})
        self.assertEqual(data["ean"], "5901234123457")
        self.assertEqual(data["brand"], "Apple")
        self.assertEqual(data["manufacturer_code"], "MQ6K3")
        self.assertEqual(data["model"], "iPhone 16")
        self.assertIsNotNone(data["specs_json"])
        self.assertIn("ean", data["specs_json"])


if __name__ == "__main__":
    unittest.main()
