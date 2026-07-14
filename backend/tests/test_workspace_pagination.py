"""Tests for paginated competitor workspace product queries."""

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import (
    Competitor,
    CompetitorCategory,
    CompetitorProduct,
    PriceSnapshot,
    Product,
    ProductMatch,
)
from app.services.workspace_query import (
    WorkspaceQueryParams,
    clamp_workspace_limit,
    list_category_workspace_page,
    list_competitor_workspace_page,
)


class WorkspacePaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        for table in (PriceSnapshot.__table__, CompetitorProduct.__table__, ProductMatch.__table__):
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

        self.competitor = Competitor(name="Technopolis", domain="technopolis.bg", currency="BGN")
        self.db.add(self.competitor)
        self.db.flush()

        self.category = CompetitorCategory(
            competitor_id=self.competitor.id,
            name="Phones",
            url="https://www.technopolis.bg/bg/phones/",
            level=0,
        )
        self.db.add(self.category)
        self.db.flush()

        self.catalog = Product(sku="SKU-1", name="Catalog Phone", brand="ACME")
        self.db.add(self.catalog)
        self.db.flush()

        now = datetime.now(timezone.utc)
        self.products: list[CompetitorProduct] = []
        for i in range(5):
            cp = CompetitorProduct(
                competitor_id=self.competitor.id,
                competitor_category_id=self.category.id if i < 4 else None,
                url=f"https://www.technopolis.bg/bg/item/p/{1000 + i}",
                title=f"Product {i}",
                product_id=self.catalog.id if i == 0 else None,
            )
            self.db.add(cp)
            self.db.flush()
            self.products.append(cp)

            snap = PriceSnapshot(
                competitor_product_id=cp.id,
                price=Decimal("100.00") + i,
                promo_price=Decimal("90.00") if i == 1 else None,
                currency="BGN",
                availability="in_stock",
                captured_at=now,
            )
            self.db.add(snap)
            cp.latest_price = snap.price
            cp.latest_promo_price = snap.promo_price
            cp.latest_currency = snap.currency
            cp.latest_availability = snap.availability
            cp.latest_scraped_at = now
            cp.latest_scrape_status = "scraped"

            if i == 2:
                self.db.add(
                    ProductMatch(
                        product_id=self.catalog.id,
                        competitor_product_id=cp.id,
                        match_score=Decimal("0.95000"),
                        match_method="ean",
                        status="auto_matched",
                        matched_by="ean_exact",
                        match_reason="Auto matched (ean_exact, score 95)",
                        match_warnings=["check specs"],
                        candidate_count=2,
                        top_candidates=[
                            {
                                "product_id": str(self.catalog.id),
                                "sku": self.catalog.sku,
                                "name": self.catalog.name,
                                "match_score": "95",
                                "match_method": "ean",
                            },
                        ],
                    ),
                )
            if i == 3:
                self.db.add(
                    ProductMatch(
                        product_id=self.catalog.id,
                        competitor_product_id=cp.id,
                        match_score=Decimal("0.00000"),
                        match_method="manual_reject",
                        status="rejected",
                        match_reason="Rejected manually",
                    ),
                )

        self.db.commit()

        session_factory = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        def override_get_db():
            db = session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def test_clamp_workspace_limit(self) -> None:
        self.assertEqual(clamp_workspace_limit(75), 75)
        self.assertEqual(clamp_workspace_limit(200), 100)
        self.assertEqual(clamp_workspace_limit(0), 1)

    def test_category_pagination_total_and_has_more(self) -> None:
        page = list_category_workspace_page(
            self.db,
            self.category.id,
            WorkspaceQueryParams(limit=2, offset=0),
        )
        assert page is not None
        self.assertEqual(page.total, 4)
        self.assertEqual(page.limit, 2)
        self.assertEqual(page.offset, 0)
        self.assertEqual(len(page.rows), 2)
        self.assertTrue(page.has_more)

        page2 = list_category_workspace_page(
            self.db,
            self.category.id,
            WorkspaceQueryParams(limit=2, offset=2),
        )
        assert page2 is not None
        self.assertEqual(len(page2.rows), 2)
        self.assertFalse(page2.has_more)

    def test_latest_price_uses_promo_when_present(self) -> None:
        page = list_category_workspace_page(
            self.db,
            self.category.id,
            WorkspaceQueryParams(limit=10, offset=0),
        )
        assert page is not None
        promo_row = next(r for r in page.rows if r.title == "Product 1")
        self.assertEqual(promo_row.latest_price, Decimal("90.00"))

    def test_match_and_confirmed_status(self) -> None:
        page = list_category_workspace_page(
            self.db,
            self.category.id,
            WorkspaceQueryParams(limit=10, offset=0),
        )
        assert page is not None
        confirmed = next(r for r in page.rows if r.title == "Product 0")
        auto = next(r for r in page.rows if r.title == "Product 2")
        self.assertEqual(confirmed.match_status, "confirmed")
        self.assertEqual(confirmed.matched_sku, "SKU-1")
        self.assertEqual(auto.match_status, "auto_matched")
        self.assertEqual(auto.matched_sku, "SKU-1")
        self.assertEqual(auto.matched_by, "ean_exact")
        self.assertEqual(auto.match_reason, "Auto matched (ean_exact, score 95)")
        self.assertEqual(auto.match_warnings, ["check specs"])
        self.assertEqual(auto.candidate_count, 2)
        self.assertEqual(len(auto.top_candidates), 1)
        self.assertEqual(auto.top_candidates[0].sku, "SKU-1")

    def test_rejected_status_is_filterable_and_not_no_candidate(self) -> None:
        rejected_page = list_category_workspace_page(
            self.db,
            self.category.id,
            WorkspaceQueryParams(limit=10, offset=0, status="rejected"),
        )
        assert rejected_page is not None
        self.assertEqual(rejected_page.total, 1)
        self.assertEqual(rejected_page.rows[0].title, "Product 3")
        self.assertEqual(rejected_page.rows[0].match_status, "rejected")

        no_candidate_page = list_category_workspace_page(
            self.db,
            self.category.id,
            WorkspaceQueryParams(limit=10, offset=0, status="no_candidate"),
        )
        assert no_candidate_page is not None
        self.assertNotIn("Product 3", {row.title for row in no_candidate_page.rows})

    def test_api_listing_returns_match_metadata(self) -> None:
        res = self.client.get(
            f"/competitor-categories/{self.category.id}/products",
            params={"limit": 10, "offset": 0},
        )
        self.assertEqual(res.status_code, 200)
        auto = next(r for r in res.json()["rows"] if r["title"] == "Product 2")
        self.assertEqual(auto["match_status"], "auto_matched")
        self.assertEqual(auto["matched_by"], "ean_exact")
        self.assertEqual(auto["match_reason"], "Auto matched (ean_exact, score 95)")
        self.assertEqual(auto["candidate_count"], 2)
        self.assertEqual(len(auto["top_candidates"]), 1)

    def test_competitor_endpoint_includes_uncategorized(self) -> None:
        page = list_competitor_workspace_page(
            self.db,
            self.competitor.id,
            WorkspaceQueryParams(limit=10, offset=0),
        )
        assert page is not None
        self.assertEqual(page.total, 5)
        uncategorized = next(r for r in page.rows if r.title == "Product 4")
        self.assertEqual(uncategorized.category_path, ["Uncategorized"])

    def test_api_limit_cap(self) -> None:
        res = self.client.get(
            f"/competitor-categories/{self.category.id}/products",
            params={"limit": 500, "offset": 0},
        )
        self.assertEqual(res.status_code, 422)

    def test_api_paginated_response_shape(self) -> None:
        res = self.client.get(
            f"/competitors/{self.competitor.id}/products",
            params={"limit": 3, "offset": 0},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["limit"], 3)
        self.assertEqual(len(body["rows"]), 3)
        self.assertTrue(body["has_more"])


if __name__ == "__main__":
    unittest.main()
