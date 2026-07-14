"""Tests for Technopolis OCC API fast scraper."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.scrapers.sites.technopolis_occ_api import (
    extract_product_code,
    map_breadcrumb_categories,
    map_classifications_to_specs_json,
    map_stock_availability,
    parse_occ_product_payload,
    pick_image_url,
    scrape_technopolis_occ,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_14251 = REPO_ROOT / "docs" / "audits" / "fixtures" / "occ_product_14251.json"
FIXTURE_16307 = REPO_ROOT / "docs" / "audits" / "fixtures" / "occ_product_16307.json"

PDP_URL = "https://www.technopolis.bg/bg/TV-stojki/TV-Stojka--HAMA-108726/p/14251"


class TechnopolisOccApiTests(unittest.TestCase):
    def test_extract_product_code(self) -> None:
        self.assertEqual(extract_product_code(PDP_URL), "14251")
        self.assertIsNone(extract_product_code("https://www.technopolis.bg/bg/"))

    def test_map_stock_availability(self) -> None:
        self.assertEqual(map_stock_availability({"stockLevelStatus": "inStock"}), "in_stock")
        self.assertEqual(map_stock_availability({"stockLevelStatus": "reserved"}), "reserved")
        self.assertEqual(map_stock_availability(None, sold_out=True), "out_of_stock")

    def test_parse_fixture_14251(self) -> None:
        self.assertTrue(FIXTURE_14251.is_file(), f"missing fixture {FIXTURE_14251}")
        payload = json.loads(FIXTURE_14251.read_text(encoding="utf-8"))
        result = parse_occ_product_payload(
            payload,
            listing_url=PDP_URL,
            captured_at=datetime.now(timezone.utc),
            product_code="14251",
        )
        assert result is not None
        self.assertEqual(result.title, "TV Стойка  HAMA 108726  ЧЕРЕН")
        self.assertEqual(result.price, Decimal("30.1"))
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(result.availability, "reserved")
        self.assertEqual(result.raw_data.get("source"), "occ_api")
        self.assertEqual(result.raw_data["stock_level"], 0)
        self.assertEqual(result.raw_data["product_identifiers"]["ean"], "4047443136848")
        self.assertIn("TV, Аудио и Gaming", result.raw_data["breadcrumb_categories"])
        self.assertTrue(result.raw_data["specs_json"])
        self.assertTrue(result.image_url and "medias" in result.image_url)

    def test_parse_fixture_16307_no_ean(self) -> None:
        payload = json.loads(FIXTURE_16307.read_text(encoding="utf-8"))
        result = parse_occ_product_payload(
            payload,
            listing_url="https://www.technopolis.bg/bg/TV-aksesoari-drugi/PCMCI-CARD-CONAX-CAM/p/16307",
            captured_at=datetime.now(timezone.utc),
            product_code="16307",
        )
        assert result is not None
        self.assertEqual(result.price, Decimal("30.5"))
        self.assertNotIn("ean", result.raw_data.get("product_identifiers") or {})

    def test_parse_returns_none_without_price(self) -> None:
        payload = {"name": "Test", "price": {}}
        self.assertIsNone(
            parse_occ_product_payload(
                payload,
                listing_url=PDP_URL,
                captured_at=datetime.now(timezone.utc),
                product_code="1",
            ),
        )

    def test_breadcrumb_excludes_pdp_leaf(self) -> None:
        crumbs = map_breadcrumb_categories(
            [
                {"name": "Cat", "url": "/c/P1"},
                {"name": "Product", "url": "/foo/p/14251"},
            ],
        )
        self.assertEqual(crumbs, ["Cat"])

    def test_classifications_to_specs(self) -> None:
        specs = map_classifications_to_specs_json(
            [{"name": "Group", "features": [{"name": "МОДЕЛ", "featureValues": [{"value": "108726"}]}]}],
        )
        self.assertEqual(specs, {"Group: МОДЕЛ": "108726"})

    def test_pick_image_url_prefers_product_format(self) -> None:
        url = pick_image_url(
            [
                {"format": "videoluxZoom", "url": "/medias/zoom.jpg"},
                {"format": "videoluxProduct", "url": "/medias/product.jpg"},
            ],
        )
        self.assertIn("product.jpg", url or "")

    @patch("app.scrapers.sites.technopolis_occ_api.fetch_occ_product", new_callable=AsyncMock)
    def test_scrape_technopolis_occ_success(self, mock_fetch: AsyncMock) -> None:
        mock_fetch.return_value = (200, json.loads(FIXTURE_14251.read_text(encoding="utf-8")), None)
        import asyncio

        result, diag = asyncio.run(scrape_technopolis_occ(PDP_URL))
        assert result is not None
        self.assertEqual(result.price, Decimal("30.1"))
        self.assertNotIn("occ_fallback_reason", diag)

    @patch("app.scrapers.sites.technopolis_occ_api.fetch_occ_product", new_callable=AsyncMock)
    def test_scrape_technopolis_occ_fallback_on_404(self, mock_fetch: AsyncMock) -> None:
        mock_fetch.return_value = (404, None, "not found")
        import asyncio

        result, diag = asyncio.run(scrape_technopolis_occ(PDP_URL))
        self.assertIsNone(result)
        self.assertEqual(diag.get("occ_fallback_reason"), "status_404")

    @patch("app.scrapers.sites.technopolis_hybrid.scrape_technopolis_occ", new_callable=AsyncMock)
    @patch("app.scrapers.sites.technopolis_hybrid.get_settings")
    def test_hybrid_uses_occ_before_playwright(self, mock_settings, mock_occ: AsyncMock) -> None:
        from app.config import Settings
        from app.scrapers.base import ScrapeResult
        from app.scrapers.sites.technopolis_hybrid import scrape_technopolis_url

        mock_settings.return_value = Settings(scrape_occ_enabled=True, scrape_http_enabled=False)
        mock_occ.return_value = (
            ScrapeResult(
                title="T",
                price=Decimal("9.99"),
                old_price=None,
                promo_price=None,
                currency="EUR",
                availability="in_stock",
                captured_at=datetime.now(timezone.utc),
                raw_data={"source": "occ_api"},
            ),
            {"occ_api_duration_ms": 120},
        )
        pool = MagicMock()
        pool.fetch_page_data = AsyncMock()

        import asyncio

        result = asyncio.run(scrape_technopolis_url(PDP_URL, pool=pool))
        self.assertEqual(result.raw_data.get("scrape_layer"), "occ_api")
        self.assertEqual(result.price, Decimal("9.99"))
        pool.fetch_page_data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
