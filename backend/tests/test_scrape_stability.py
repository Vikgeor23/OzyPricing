"""Tests for Playwright stability pass (adaptive concurrency, dead URLs, URL health)."""

import unittest
import uuid
from datetime import datetime, timezone
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
from app.scrapers.sites.technopolis_playwright_pool import PlaywrightFetchResult
from app.services.adaptive_concurrency import AdaptiveConcurrencyController
from app.services.scrape_errors import (
    SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT,
    SCRAPE_ERROR_PRICE_NOT_FOUND,
    SCRAPE_ERROR_PRODUCT_NOT_FOUND,
)
from app.services.scrape_persist import apply_scrape_result_to_listing
from app.services.scraping_batch import _count_scrape_targets, _scrape_ids_stmt
from app.services.url_health import update_url_health_after_scrape


def _settings(**overrides) -> Settings:
    base = {
        "scrape_concurrency": 12,
        "scrape_concurrency_min": 6,
        "scrape_concurrency_max": 20,
        "scrape_adaptive_window_high": 100,
        "scrape_adaptive_window_low": 200,
        "scrape_adaptive_timeout_rate_high": 0.30,
        "scrape_adaptive_timeout_rate_low": 0.10,
        "scrape_skip_dead_urls": True,
        "scrape_http_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


class AdaptiveConcurrencyTests(unittest.TestCase):
    @patch("app.services.adaptive_concurrency.get_settings")
    def test_reduces_on_high_timeout_rate(self, mock_settings) -> None:
        mock_settings.return_value = _settings(scrape_concurrency=14)
        ctrl = AdaptiveConcurrencyController(initial=14)
        for _ in range(100):
            ctrl.record_outcome(timed_out=True)
        # Multiplicative cut: 14 - max(2, 14 // 4) = 11.
        self.assertEqual(ctrl.current_limit, 11)

    @patch("app.services.adaptive_concurrency.get_settings")
    def test_increases_on_low_timeout_rate_legacy(self, mock_settings) -> None:
        mock_settings.return_value = _settings(
            scrape_concurrency=10,
            scrape_adaptive_throughput_enabled=False,
        )
        ctrl = AdaptiveConcurrencyController(initial=10)
        for _ in range(200):
            ctrl.record_outcome(timed_out=False)
        self.assertEqual(ctrl.current_limit, 11)

    @patch("app.services.adaptive_concurrency.get_settings")
    def test_respects_min_max(self, mock_settings) -> None:
        mock_settings.return_value = _settings()
        ctrl = AdaptiveConcurrencyController(initial=6)
        for _ in range(500):
            ctrl.record_outcome(timed_out=True)
        self.assertEqual(ctrl.current_limit, 6)

        ctrl2 = AdaptiveConcurrencyController(initial=20)
        for _ in range(500):
            ctrl2.record_outcome(timed_out=False)
        self.assertEqual(ctrl2.current_limit, 20)

    def _run_throughput_sim(self, capacity: int, *, max_limit: int, sim_hours: float = 2.0) -> list[int]:
        """Feed outcomes with a fake clock: the site saturates at `capacity`."""
        import app.services.adaptive_concurrency as ac

        fake_now = [0.0]
        with patch.object(ac.time, "monotonic", lambda: fake_now[0]):
            ctrl = AdaptiveConcurrencyController(initial=12, min_limit=6, max_limit=max_limit)
            history = []
            while fake_now[0] < sim_hours * 3600:
                effective = min(ctrl.current_limit, capacity)
                fake_now[0] += 2.0 / effective  # 2s service time per request
                ctrl.record_outcome(timed_out=False)
                history.append(ctrl.current_limit)
        return history

    @patch("app.services.adaptive_concurrency.get_settings")
    def test_throughput_settles_at_site_capacity(self, mock_settings) -> None:
        mock_settings.return_value = _settings(scrape_adaptive_throughput_enabled=True)
        history = self._run_throughput_sim(24, max_limit=128)
        tail = history[len(history) // 2 :]
        # Settles near the capacity knee instead of climbing to max_limit.
        self.assertLessEqual(max(tail), 32)
        self.assertGreaterEqual(sum(tail) / len(tail), 20)

    @patch("app.services.adaptive_concurrency.get_settings")
    def test_throughput_climbs_while_it_pays(self, mock_settings) -> None:
        mock_settings.return_value = _settings(scrape_adaptive_throughput_enabled=True)
        history = self._run_throughput_sim(1000, max_limit=48)
        # No knee below the cap: should reach the configured ceiling.
        self.assertEqual(max(history), 48)


class UrlHealthTests(unittest.TestCase):
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

    def _cp(self) -> CompetitorProduct:
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/1",
        )
        self.db.add(cp)
        self.db.flush()
        return cp

    def test_dead_after_three_timeouts(self) -> None:
        cp = self._cp()
        for _ in range(3):
            update_url_health_after_scrape(
                cp,
                outcome="failed",
                error_code=SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT,
            )
        self.assertTrue(cp.is_dead)
        self.assertEqual(cp.consecutive_timeout_count, 3)

    def test_dead_after_two_product_not_found(self) -> None:
        cp = self._cp()
        for _ in range(2):
            update_url_health_after_scrape(
                cp,
                outcome="failed",
                error_code=SCRAPE_ERROR_PRODUCT_NOT_FOUND,
            )
        self.assertTrue(cp.is_dead)

    def test_price_not_found_does_not_mark_dead(self) -> None:
        cp = self._cp()
        for _ in range(5):
            update_url_health_after_scrape(
                cp,
                outcome="failed",
                error_code=SCRAPE_ERROR_PRICE_NOT_FOUND,
            )
        self.assertFalse(cp.is_dead)
        self.assertEqual(cp.consecutive_not_found_count, 0)

    def test_success_resets_streaks(self) -> None:
        cp = self._cp()
        cp.consecutive_timeout_count = 2
        update_url_health_after_scrape(cp, outcome="scraped", error_code=None)
        self.assertEqual(cp.consecutive_timeout_count, 0)


class DeadUrlBatchFilterTests(unittest.TestCase):
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

    def test_skip_dead_urls_in_batch_targets(self) -> None:
        alive = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/alive",
            is_dead=False,
        )
        dead = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/dead",
            is_dead=True,
        )
        self.db.add_all([alive, dead])
        self.db.commit()

        count = _count_scrape_targets(
            self.db,
            competitor_id=self.competitor.id,
            category_id=None,
            only_missing=False,
            only_stale=False,
            stale_hours=24,
            skip_recent_failures=False,
            recent_failure_hours=24,
            skip_dead_urls=True,
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
                    skip_recent_failures=False,
                    recent_failure_hours=24,
                    skip_dead_urls=True,
                ),
            ).all(),
        )
        self.assertEqual(ids, [alive.id])


class HybridRetryTests(unittest.TestCase):
    @patch("app.scrapers.sites.technopolis_hybrid.get_settings")
    @patch("app.scrapers.sites.technopolis_hybrid.fetch_technopolis_html_http", new_callable=AsyncMock)
    @patch("app.scrapers.sites.technopolis.TechnopolisScraper._parse_html_to_result")
    def test_retries_once_on_timeout(self, mock_parse, mock_http, mock_settings) -> None:
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
        timeout_result = PlaywrightFetchResult(timed_out=True, error="timeout")
        ok_result = PlaywrightFetchResult(
            js_extract={"priceText": "10,00 лв", "title": "Phone"},
            diagnostics={"parse_mode": "js_evaluate"},
        )
        pool.fetch_page_data = AsyncMock(side_effect=[timeout_result, ok_result])

        import asyncio
        from app.scrapers.sites.technopolis_hybrid import scrape_technopolis_url

        url = "https://www.technopolis.bg/bg/phones/x/p/12345"
        with patch(
            "app.scrapers.sites.technopolis_hybrid.parse_js_extract_payload",
        ) as mock_js:
            mock_js.return_value = ScrapeResult(
                title="Phone",
                price=Decimal("10"),
                old_price=None,
                promo_price=None,
                currency="BGN",
                availability="in_stock",
                captured_at=datetime.now(timezone.utc),
                raw_data={"parse_mode": "js_evaluate"},
            )
            result = asyncio.run(scrape_technopolis_url(url, pool=pool))

        self.assertEqual(pool.fetch_page_data.await_count, 2)
        self.assertTrue(result.raw_data.get("playwright_retry"))
        mock_http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
