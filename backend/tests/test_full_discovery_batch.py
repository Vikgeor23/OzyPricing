"""Tests for incremental full-domain product URL discovery."""

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import JSON, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Competitor, CompetitorCategory, CompetitorProduct
from app.services.full_discovery_batch import _dedupe_discovered_urls, run_incremental_full_discovery

TECHNOPOLIS = "technopolis.bg"
URL_A = "https://www.technopolis.bg/bg/phones/iphone/p/1001"
URL_B = "https://www.technopolis.bg/bg/tvs/samsung-tv/p/2002"
URL_A_EN = "https://www.technopolis.bg/en/phones/iphone/p/1001"


class FullDiscoveryBatchTests(unittest.TestCase):
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
            tables=[
                Competitor.__table__,
                CompetitorCategory.__table__,
                CompetitorProduct.__table__,
            ],
        )
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.db = self.Session()

        self.competitor = Competitor(name="Technopolis", domain=TECHNOPOLIS, currency="BGN")
        self.db.add(self.competitor)
        self.db.flush()

        self.cat = CompetitorCategory(
            competitor_id=self.competitor.id,
            name="Phones",
            url="https://www.technopolis.bg/bg/phones/",
            level=0,
        )
        self.db.add(self.cat)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()

    def _mock_sitemap(self, urls: list[str]):
        return patch(
            "app.services.full_discovery_batch.collect_product_urls_from_sitemaps",
            return_value=(urls, {"sitemap_urls_checked": 1, "errors": []}),
        )

    def test_dedupe_by_product_code_prefers_bg(self) -> None:
        listings = _dedupe_discovered_urls([URL_A_EN, URL_A])
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].url, URL_A)
        self.assertEqual(listings[0].product_code, "1001")

    def test_only_new_creates_missing_only(self) -> None:
        existing = CompetitorProduct(
            competitor_id=self.competitor.id,
            url=URL_A,
            technopolis_product_code="1001",
            latest_price=Decimal("99.00"),
            latest_scraped_at=datetime.now(timezone.utc),
        )
        self.db.add(existing)
        self.db.commit()

        with self._mock_sitemap([URL_A, URL_B]):
            result = run_incremental_full_discovery(
                self.db,
                self.competitor.id,
                only_new=True,
                force_rescan=False,
                source="sitemap",
            )

        self.assertEqual(result["product_urls_found"], 2)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped_existing"], 1)
        self.assertEqual(result["new_urls_found"], 1)

        rows = self.db.scalars(
            select(CompetitorProduct).where(CompetitorProduct.competitor_id == self.competitor.id),
        ).all()
        self.assertEqual(len(rows), 2)
        kept = self.db.scalars(
            select(CompetitorProduct).where(CompetitorProduct.url == URL_A),
        ).one()
        self.assertEqual(kept.latest_price, Decimal("99.00"))

    def test_force_rescan_does_not_duplicate(self) -> None:
        existing = CompetitorProduct(
            competitor_id=self.competitor.id,
            url=URL_A,
            technopolis_product_code="1001",
        )
        self.db.add(existing)
        self.db.commit()

        with self._mock_sitemap([URL_A, URL_B]):
            result = run_incremental_full_discovery(
                self.db,
                self.competitor.id,
                only_new=False,
                force_rescan=True,
                source="sitemap",
            )

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped_existing"], 1)
        count = self.db.scalars(
            select(CompetitorProduct).where(CompetitorProduct.competitor_id == self.competitor.id),
        ).all()
        self.assertEqual(len(count), 2)

    def test_category_fallback_for_existing_without_category(self) -> None:
        existing = CompetitorProduct(
            competitor_id=self.competitor.id,
            url=URL_A,
            technopolis_product_code="1001",
            competitor_category_id=None,
        )
        self.db.add(existing)
        self.db.commit()

        with self._mock_sitemap([URL_A]):
            result = run_incremental_full_discovery(
                self.db,
                self.competitor.id,
                only_new=True,
                force_rescan=False,
                source="sitemap",
            )

        self.assertEqual(result["created"], 0)
        self.assertGreaterEqual(result["categories_updated"], 1)
        refreshed = self.db.scalars(
            select(CompetitorProduct).where(CompetitorProduct.url == URL_A),
        ).one()
        self.assertIsNotNone(refreshed.competitor_category_id)

    def test_product_code_dedupe_skips_second_url(self) -> None:
        alt_url = "https://www.technopolis.bg/en/phones/iphone-alt/p/1001"
        existing = CompetitorProduct(
            competitor_id=self.competitor.id,
            url=URL_A,
            technopolis_product_code="1001",
        )
        self.db.add(existing)
        self.db.commit()

        with self._mock_sitemap([alt_url]):
            result = run_incremental_full_discovery(
                self.db,
                self.competitor.id,
                only_new=True,
                source="sitemap",
            )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped_existing"], 1)
        rows = self.db.scalars(
            select(CompetitorProduct).where(CompetitorProduct.competitor_id == self.competitor.id),
        ).all()
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
