"""Tests for batch competitor product scraping."""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.scrapers.base import ScrapeResult

from sqlalchemy import JSON, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Competitor, CompetitorCategory, CompetitorProduct, PriceSnapshot, Product, ProductMatch
from app.services.scraping_batch import run_batch_scrape_competitor_products
from app.services.workspace_query import WorkspaceQueryParams, list_category_workspace_page


class BatchScrapeTests(unittest.TestCase):
    def setUp(self) -> None:
        for table in (CompetitorProduct.__table__, PriceSnapshot.__table__):
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
            tables=[
                Product.__table__,
                Competitor.__table__,
                CompetitorCategory.__table__,
                CompetitorProduct.__table__,
                PriceSnapshot.__table__,
                ProductMatch.__table__,
            ],
        )
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.db = self.Session()

        self.competitor = Competitor(name="Shop", domain="shop.test", currency="BGN")
        self.db.add(self.competitor)
        self.db.flush()

        self.cat_a = CompetitorCategory(
            competitor_id=self.competitor.id,
            name="Phones",
            url="https://shop.test/phones/",
            level=0,
        )
        self.cat_b = CompetitorCategory(
            competitor_id=self.competitor.id,
            name="TVs",
            url="https://shop.test/tvs/",
            level=0,
        )
        self.db.add_all([self.cat_a, self.cat_b])
        self.db.flush()

        now = datetime.now(timezone.utc)
        stale = now - timedelta(hours=48)

        self.cp_missing = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=self.cat_a.id,
            url="https://shop.test/p/missing",
            title="Missing",
        )
        self.cp_scraped = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=self.cat_a.id,
            url="https://shop.test/p/scraped",
            title="Scraped",
            last_seen_at=now,
            latest_price=10,
            latest_currency="BGN",
            latest_scraped_at=now,
            latest_scrape_status="scraped",
        )
        self.cp_stale = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=self.cat_b.id,
            url="https://shop.test/p/stale",
            title="Stale",
            last_seen_at=stale,
        )
        self.db.add_all([self.cp_missing, self.cp_scraped, self.cp_stale])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _ok_result(self) -> ScrapeResult:
        return ScrapeResult(
            title="T",
            price=10,
            old_price=None,
            promo_price=None,
            currency="BGN",
            availability="in_stock",
            captured_at=datetime.now(timezone.utc),
            raw_data={"scraper_status": "success", "scrape_layer": "http"},
        )

    @patch("app.services.scraping_batch.fetch_scrape_result_for_listing", new_callable=AsyncMock)
    def test_batch_continues_on_failed_url(self, mock_fetch) -> None:
        mock_fetch.side_effect = [
            self._ok_result(),
            self._ok_result(),
            RuntimeError("boom"),
        ]

        result = run_batch_scrape_competitor_products(
            self.db,
            competitor_id=self.competitor.id,
        )

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["scraped"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(mock_fetch.await_count, 3)

    @patch("app.services.scraping_batch.fetch_scrape_result_for_listing", new_callable=AsyncMock)
    def test_only_missing_limits_scope(self, mock_fetch) -> None:
        mock_fetch.return_value = self._ok_result()
        result = run_batch_scrape_competitor_products(
            self.db,
            competitor_id=self.competitor.id,
            only_missing=True,
        )

        # missing listing + stale listing without snapshot
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["scraped"], 2)

    @patch("app.services.scraping_batch.fetch_scrape_result_for_listing", new_callable=AsyncMock)
    def test_category_id_limits_scope(self, mock_fetch) -> None:
        mock_fetch.return_value = self._ok_result()
        result = run_batch_scrape_competitor_products(
            self.db,
            competitor_id=self.competitor.id,
            category_id=self.cat_a.id,
        )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["scraped"], 2)

    def test_workspace_sort_last_checked_desc_nulls_last(self) -> None:
        page = list_category_workspace_page(
            self.db,
            self.cat_a.id,
            WorkspaceQueryParams(limit=10, offset=0, sort_by="last_scraped_at", sort_dir="desc"),
        )
        self.assertIsNotNone(page)
        assert page is not None
        ids = [r.competitor_product_id for r in page.rows]
        self.assertEqual(ids[0], self.cp_scraped.id)
        self.assertEqual(ids[-1], self.cp_missing.id)

    def test_scraped_filter(self) -> None:
        page = list_category_workspace_page(
            self.db,
            self.cat_a.id,
            WorkspaceQueryParams(limit=10, scraped=False),
        )
        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual(page.total, 1)
        self.assertEqual(page.rows[0].competitor_product_id, self.cp_missing.id)


if __name__ == "__main__":
    unittest.main()
