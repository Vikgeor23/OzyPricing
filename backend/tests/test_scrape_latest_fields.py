"""Tests for latest_* scrape fields and optional price history."""

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import JSON, create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.models import Competitor, CompetitorCategory, CompetitorProduct, PriceSnapshot, Product, ProductMatch
from app.scrapers.base import ScrapeResult
from app.services.scrape_persist import apply_scrape_result_to_listing
from app.services.workspace_query import WorkspaceQueryParams, list_competitor_workspace_page


def _sample_result() -> ScrapeResult:
    return ScrapeResult(
        title="Phone X",
        price=Decimal("99.99"),
        old_price=Decimal("119.99"),
        promo_price=Decimal("89.99"),
        currency="BGN",
        availability="in_stock",
        captured_at=datetime.now(timezone.utc),
        image_url="https://cdn.test/img.jpg",
        raw_data={"scraper_status": "ok"},
    )


class ScrapeLatestFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        tables = [
            Product.__table__,
            Competitor.__table__,
            CompetitorCategory.__table__,
            CompetitorProduct.__table__,
            PriceSnapshot.__table__,
            ProductMatch.__table__,
        ]
        # SQLite can't render Postgres JSONB; swap every JSONB column across the
        # tables we create for a portable JSON type.
        for table in tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = JSON()

        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine, tables=tables)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.db = self.Session()

        self.competitor = Competitor(name="Shop", domain="shop.test", currency="BGN")
        self.db.add(self.competitor)
        self.db.flush()

        self.cp_new = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://shop.test/p/new",
            title="Old title",
        )
        self.cp_old = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://shop.test/p/old",
            title="Older",
            latest_scraped_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            latest_price=Decimal("1"),
            latest_scrape_status="scraped",
        )
        self.db.add_all([self.cp_new, self.cp_old])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    @patch("app.services.scrape_persist.get_settings")
    def test_scrape_updates_latest_fields_no_snapshot_when_history_disabled(self, mock_settings) -> None:
        mock_settings.return_value = Settings(price_history_enabled=False)
        result = _sample_result()

        outcome = apply_scrape_result_to_listing(
            self.db,
            self.cp_new,
            result,
            listing_url=self.cp_new.url,
            task_duration_ms=10,
            competitor_product_id=str(self.cp_new.id),
        )
        self.db.commit()

        self.assertEqual(outcome, "scraped")
        refreshed = self.db.get(CompetitorProduct, self.cp_new.id)
        assert refreshed is not None
        self.assertEqual(refreshed.latest_promo_price, Decimal("89.99"))
        self.assertEqual(refreshed.latest_scrape_status, "scraped")
        self.assertIsNotNone(refreshed.latest_scraped_at)
        self.assertEqual(
            int(self.db.scalar(select(func.count()).select_from(PriceSnapshot)) or 0),
            0,
        )

    @patch("app.services.scrape_persist.get_settings")
    def test_scrape_creates_snapshot_when_history_enabled(self, mock_settings) -> None:
        mock_settings.return_value = Settings(price_history_enabled=True)
        apply_scrape_result_to_listing(
            self.db,
            self.cp_new,
            _sample_result(),
            listing_url=self.cp_new.url,
            task_duration_ms=10,
            competitor_product_id=str(self.cp_new.id),
        )
        self.db.commit()
        count = int(self.db.scalar(select(func.count()).select_from(PriceSnapshot)) or 0)
        self.assertEqual(count, 1)

    @patch("app.services.scrape_persist.get_settings")
    def test_variant_expansion_creates_siblings_and_is_idempotent(self, mock_settings) -> None:
        mock_settings.return_value = Settings(
            price_history_enabled=False,
            scrape_expand_variants=True,
        )
        parent_url = "https://shop.test/p-333/"
        parent = CompetitorProduct(
            competitor_id=self.competitor.id,
            url=parent_url,
            brand="Bioderma",
            image_url="https://cdn.test/parent.jpg",
        )
        self.db.add(parent)
        self.db.flush()

        result = ScrapeResult(
            title="Atoderm Gel",
            price=Decimal("12.40"),
            old_price=Decimal("12.40"),
            promo_price=Decimal("10.60"),
            currency="EUR",
            availability="in_stock",
            captured_at=datetime.now(timezone.utc),
            raw_data={"scraper_status": "ok"},
            variants=[
                {
                    "url": "https://shop.test/p-111/", "size": "1000ML",
                    "price": Decimal("18.8"), "regular": Decimal("23.7"),
                    "currency": "EUR", "ean": "3701129811542",
                    "manufacturer_code": "MFR-1000", "shop_code": "BIR0755",
                    "title": "Atoderm Gel",
                },
                {
                    "url": "https://shop.test/p-222/", "size": "500ML",
                    "price": Decimal("14.8"), "regular": Decimal("17.8"),
                    "currency": "EUR", "ean": "3701129811573",
                    "manufacturer_code": "MFR-0500", "shop_code": "BIR0561",
                    "title": "Atoderm Gel",
                },
            ],
        )

        for _ in range(2):
            apply_scrape_result_to_listing(
                self.db,
                parent,
                result,
                listing_url=parent_url,
                task_duration_ms=10,
                competitor_product_id=str(parent.id),
            )
            self.db.commit()

        siblings = (
            self.db.query(CompetitorProduct)
            .filter(CompetitorProduct.discovery_source == "variant_expansion")
            .all()
        )
        self.assertEqual(len(siblings), 2)  # idempotent: two runs, still two rows
        by_size = {(s.specs_json or {}).get("size"): s for s in siblings}
        self.assertEqual(set(by_size), {"1000ML", "500ML"})
        s500 = by_size["500ML"]
        self.assertEqual(s500.latest_price, Decimal("17.8"))
        self.assertEqual(s500.latest_promo_price, Decimal("14.8"))
        self.assertEqual(s500.ean, "3701129811573")
        self.assertEqual(s500.shop_code, "BIR0561")
        self.assertEqual(s500.competitor_id, self.competitor.id)
        self.assertEqual(s500.image_url, "https://cdn.test/parent.jpg")

    def test_listing_sorts_latest_scraped_at_desc_nulls_last(self) -> None:
        page = list_competitor_workspace_page(
            self.db,
            self.competitor.id,
            WorkspaceQueryParams(limit=10, sort_by="last_scraped_at", sort_dir="desc"),
        )
        assert page is not None
        ids = [r.competitor_product_id for r in page.rows]
        self.assertEqual(ids[0], self.cp_old.id)
        self.assertEqual(ids[-1], self.cp_new.id)

    def test_scraped_filter_uses_latest_scraped_at(self) -> None:
        page = list_competitor_workspace_page(
            self.db,
            self.competitor.id,
            WorkspaceQueryParams(limit=10, scraped=False),
        )
        assert page is not None
        self.assertEqual(page.total, 1)
        self.assertEqual(page.rows[0].competitor_product_id, self.cp_new.id)


if __name__ == "__main__":
    unittest.main()
