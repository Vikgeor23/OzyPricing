"""Tests for paginated list endpoints and matching catalog prefilter."""

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.db.pagination import clamp_limit
from app.main import app
from app.models import Competitor, CompetitorProduct, Product, ProductMatch
from app.services.matching_catalog import fetch_catalog_candidates_for_listing


class ListPaginationTests(unittest.TestCase):
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
                Product.__table__,
                Competitor.__table__,
                CompetitorProduct.__table__,
                ProductMatch.__table__,
            ],
        )
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.db = self.Session()

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        for i in range(12):
            self.db.add(Product(sku=f"SKU-{i}", name=f"Product {i}", ean=f"ean-{i}" if i < 3 else None))
        self.db.commit()

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.db.close()

    def test_clamp_limit(self) -> None:
        self.assertEqual(clamp_limit(75), 75)
        self.assertEqual(clamp_limit(200), 100)
        self.assertEqual(clamp_limit(0), 1)

    def test_list_products_paginated(self) -> None:
        r = self.client.get("/products?limit=5&offset=0")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["rows"]), 5)
        self.assertEqual(body["total"], 12)
        self.assertTrue(body["has_more"])

        r2 = self.client.get("/products?limit=5&offset=10")
        self.assertEqual(len(r2.json()["rows"]), 2)
        self.assertFalse(r2.json()["has_more"])

    def test_price_comparison_paginated(self) -> None:
        r = self.client.get("/products/price-comparison?limit=10&offset=0")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["rows"]), 10)
        self.assertEqual(body["total"], 12)

    def test_match_catalog_ean_prefilter(self) -> None:
        comp = Competitor(name="Shop", domain="shop.bg", currency="BGN")
        self.db.add(comp)
        self.db.flush()
        target = self.db.scalars(select(Product).where(Product.ean == "ean-1")).first()
        cp = CompetitorProduct(
            competitor_id=comp.id,
            url="https://shop.bg/item/1",
            ean="ean-1",
        )
        self.db.add(cp)
        self.db.commit()

        candidates = fetch_catalog_candidates_for_listing(self.db, cp)
        self.assertIsNotNone(candidates)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].id, target.id)


if __name__ == "__main__":
    unittest.main()
