"""Tests for Technopolis scrape quick wins (audit follow-up)."""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import JSON, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.models import Competitor, CompetitorCategory, CompetitorProduct
from app.scrapers.base import ScrapeResult
from app.scrapers.sites.technopolis_playwright_pool import PlaywrightFetchResult, TechnopolisPlaywrightPool
from app.services.scrape_errors import (
    SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT,
    SCRAPE_ERROR_PRICE_NOT_FOUND,
    SCRAPE_ERROR_PRODUCT_NOT_FOUND,
    classify_scrape_failure,
)
from app.services.scrape_persist import apply_scrape_result_to_listing
from app.services.scraping_batch import _count_scrape_targets, _scrape_ids_stmt, run_batch_scrape_competitor_products


def _settings(**overrides) -> Settings:
    base = {
        "scrape_http_enabled": False,
        "scrape_navigation_timeout_ms": 10_000,
        "scrape_title_wait_ms": 2_000,
        "scrape_price_selector_wait_ms": 3_000,
        "scrape_skip_recent_failures": True,
        "scrape_recent_failure_hours": 24,
    }
    base.update(overrides)
    return Settings(**base)


class ScrapeQuickWinsTests(unittest.TestCase):
    def setUp(self) -> None:
        for table in (CompetitorProduct.__table__,):
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = JSON()

        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(
            self.engine,
            tables=[Competitor.__table__, CompetitorCategory.__table__, CompetitorProduct.__table__],
        )
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.db = self.Session()

        self.competitor = Competitor(name="Technopolis", domain="technopolis.bg", currency="BGN")
        self.db.add(self.competitor)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()

    @patch("app.scrapers.sites.technopolis_hybrid.get_settings")
    @patch("app.scrapers.sites.technopolis_hybrid.fetch_technopolis_html_http", new_callable=AsyncMock)
    @patch("app.scrapers.sites.technopolis.TechnopolisScraper._parse_html_to_result")
    def test_http_skipped_when_config_false(self, mock_parse, mock_http, mock_settings) -> None:
        mock_settings.return_value = _settings(scrape_http_enabled=False)
        mock_parse.return_value = ScrapeResult(
            title="Phone",
            price=Decimal("10"),
            old_price=None,
            promo_price=None,
            currency="BGN",
            availability="in_stock",
            captured_at=datetime.now(timezone.utc),
            raw_data={},
        )
        pool = MagicMock()
        pool.fetch_page_data = AsyncMock(
            return_value=PlaywrightFetchResult(
                html="<html>" + "x" * 5000 + "</html>",
                diagnostics={},
            ),
        )

        import asyncio
        from app.scrapers.sites.technopolis_hybrid import scrape_technopolis_url

        url = "https://www.technopolis.bg/bg/phones/x/p/12345"
        result = asyncio.run(scrape_technopolis_url(url, pool=pool))

        mock_http.assert_not_called()
        self.assertTrue(result.raw_data.get("http_skipped"))
        self.assertEqual(result.raw_data.get("scrape_layer"), "playwright")
        pool.fetch_page_data.assert_called_once()

    def test_failure_code_stored_on_persist(self) -> None:
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/1",
        )
        self.db.add(cp)
        self.db.flush()

        result = ScrapeResult(
            title=None,
            price=None,
            old_price=None,
            promo_price=None,
            currency="BGN",
            availability=None,
            captured_at=datetime.now(timezone.utc),
            raw_data={
                "scraper_status": "failure",
                "error": "price_missing_after_playwright",
                "scrape_error_code": SCRAPE_ERROR_PRICE_NOT_FOUND,
            },
        )
        apply_scrape_result_to_listing(
            self.db,
            cp,
            result,
            listing_url=cp.url,
            task_duration_ms=100,
            competitor_product_id=str(cp.id),
        )
        self.db.commit()
        refreshed = self.db.get(CompetitorProduct, cp.id)
        assert refreshed is not None
        self.assertEqual(refreshed.latest_scrape_error_code, SCRAPE_ERROR_PRICE_NOT_FOUND)
        self.assertEqual(refreshed.latest_scrape_status, "failed")

    def test_recent_failures_skipped_from_batch_targets(self) -> None:
        now = datetime.now(timezone.utc)
        recent_fail = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/fail1",
            latest_scrape_status="failed",
            latest_scrape_error_code=SCRAPE_ERROR_PRODUCT_NOT_FOUND,
            latest_scraped_at=now - timedelta(hours=1),
        )
        ok = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/ok1",
            latest_scrape_status="failed",
            latest_scrape_error_code=SCRAPE_ERROR_PRODUCT_NOT_FOUND,
            latest_scraped_at=now - timedelta(hours=48),
        )
        self.db.add_all([recent_fail, ok])
        self.db.commit()

        count = _count_scrape_targets(
            self.db,
            competitor_id=self.competitor.id,
            category_id=None,
            only_missing=False,
            only_stale=False,
            stale_hours=24,
            skip_recent_failures=True,
            recent_failure_hours=24,
            skip_dead_urls=False,
        )
        self.assertEqual(count, 1)

        ids = list(
            self.db.scalars(
                _scrape_ids_stmt(
                    competitor_id=self.competitor.id,
                    category_id=None,
                    only_missing=False,
                    only_stale=False,
                    stale_hours=24,
                    skip_recent_failures=True,
                    recent_failure_hours=24,
                    skip_dead_urls=False,
                ),
            ).all(),
        )
        self.assertEqual(ids, [ok.id])

    def test_classify_playwright_timeout(self) -> None:
        code = classify_scrape_failure(
            exc=TimeoutError("Timeout 10000ms exceeded"),
            error_message="Timeout 10000ms exceeded",
        )
        self.assertEqual(code, SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT)

    def test_playwright_timeout_config_applied(self) -> None:
        settings = _settings(
            scrape_navigation_timeout_ms=10_000,
            scrape_title_wait_ms=2_000,
            scrape_price_selector_wait_ms=3_000,
        )
        self.assertEqual(settings.scrape_navigation_timeout_ms, 10_000)
        self.assertEqual(settings.scrape_title_wait_ms, 2_000)
        self.assertEqual(settings.scrape_price_selector_wait_ms, 3_000)

    @patch("app.services.scraping_batch.fetch_scrape_result_for_listing", new_callable=AsyncMock)
    @patch("app.services.scraping_batch.TechnopolisPlaywrightPool")
    def test_batch_metrics_http_skipped(self, mock_pool_cls, mock_fetch) -> None:
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/1",
        )
        self.db.add(cp)
        self.db.commit()

        mock_fetch.return_value = ScrapeResult(
            title="T",
            price=Decimal("9"),
            old_price=None,
            promo_price=None,
            currency="BGN",
            availability="in_stock",
            captured_at=datetime.now(timezone.utc),
            raw_data={
                "scraper_status": "success",
                "scrape_layer": "playwright",
                "http_skipped": True,
                "playwright_duration_ms": 5000,
            },
        )

        pool_instance = MagicMock()
        pool_instance.__aenter__ = AsyncMock(return_value=pool_instance)
        pool_instance.__aexit__ = AsyncMock(return_value=None)
        mock_pool_cls.return_value = pool_instance

        with patch("app.services.scraping_batch.get_settings", return_value=_settings()):
            result = run_batch_scrape_competitor_products(
                self.db,
                competitor_id=self.competitor.id,
            )

        self.assertEqual(result["http_skipped"], 1)
        self.assertEqual(result["avg_playwright_ms"], 5000)


if __name__ == "__main__":
    unittest.main()
