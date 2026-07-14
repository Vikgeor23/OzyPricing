"""Tests for hybrid Technopolis HTTP / Playwright scraping."""

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.scrapers.base import ScrapeResult
from app.scrapers.sites.technopolis_hybrid import (
    _is_blocked_response,
    _needs_playwright_fallback,
    scrape_technopolis_url,
)
from app.scrapers.sites.technopolis_playwright_pool import PlaywrightFetchResult


class ScrapeHybridTests(unittest.TestCase):
    def test_blocked_short_html(self) -> None:
        self.assertTrue(_is_blocked_response(200, "<html></html>"))

    def test_needs_playwright_when_price_missing(self) -> None:
        result = ScrapeResult(
            title="Phone",
            price=None,
            old_price=None,
            promo_price=None,
            currency="BGN",
            availability=None,
            captured_at=None,
            raw_data={"scraper_status": "success"},
        )
        self.assertTrue(_needs_playwright_fallback(result, "<html><body>лв 10</body></html>" * 200))

    @patch("app.scrapers.sites.technopolis_hybrid.get_settings")
    @patch("app.scrapers.sites.technopolis_hybrid.fetch_technopolis_html_http", new_callable=AsyncMock)
    @patch("app.scrapers.sites.technopolis.TechnopolisScraper._parse_html_to_result")
    def test_http_success_skips_playwright(self, mock_parse, mock_http, mock_settings) -> None:
        from app.config import Settings

        mock_settings.return_value = Settings(scrape_http_enabled=True)
        url = "https://www.technopolis.bg/bg/phones/x/p/12345"
        mock_http.return_value = ("<html>" + "x" * 5000 + "</html>", 200, None)
        mock_parse.return_value = ScrapeResult(
            title="Phone",
            price=Decimal("99.99"),
            old_price=None,
            promo_price=None,
            currency="BGN",
            availability="in_stock",
            captured_at=None,
            raw_data={},
        )

        pool = MagicMock()
        pool.fetch_page_data = AsyncMock()

        import asyncio

        result = asyncio.run(scrape_technopolis_url(url, pool=pool))

        self.assertEqual(result.raw_data.get("scrape_layer"), "http")
        self.assertEqual(result.price, Decimal("99.99"))
        pool.fetch_page_data.assert_not_called()

    @patch("app.scrapers.sites.technopolis_hybrid.get_settings")
    @patch("app.scrapers.sites.technopolis_hybrid.fetch_technopolis_html_http", new_callable=AsyncMock)
    @patch("app.scrapers.sites.technopolis.TechnopolisScraper._parse_html_to_result")
    def test_playwright_fallback_when_http_has_no_price(self, mock_parse, mock_http, mock_settings) -> None:
        from app.config import Settings

        mock_settings.return_value = Settings(scrape_http_enabled=True)
        url = "https://www.technopolis.bg/bg/phones/x/p/12345"
        mock_http.return_value = ("<html>" + "x" * 5000 + "</html>", 200, None)

        def parse_side_effect(html, **kwargs):
            if kwargs.get("extra_raw", {}).get("fetch_layer") == "http":
                return ScrapeResult(
                    title="Phone",
                    price=None,
                    old_price=None,
                    promo_price=None,
                    currency="BGN",
                    availability=None,
                    captured_at=None,
                    raw_data={},
                )
            return ScrapeResult(
                title="Phone",
                price=Decimal("50.00"),
                old_price=None,
                promo_price=None,
                currency="BGN",
                availability="in_stock",
                captured_at=None,
                raw_data={},
            )

        mock_parse.side_effect = parse_side_effect

        pool = MagicMock()
        pool.fetch_page_data = AsyncMock(
            return_value=PlaywrightFetchResult(html="<html>" + "y" * 5000 + "</html>", diagnostics={}),
        )

        import asyncio

        result = asyncio.run(scrape_technopolis_url(url, pool=pool))

        self.assertEqual(result.raw_data.get("scrape_layer"), "playwright")
        pool.fetch_page_data.assert_called_once()


if __name__ == "__main__":
    unittest.main()
