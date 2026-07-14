"""Tests for batch competitor product matching."""

import unittest
import uuid
from decimal import Decimal

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Competitor, CompetitorCategory, CompetitorProduct, Product, ProductMatch
from app.services.matching_batch import apply_match_for_competitor_product, run_batch_match_competitor_products


class BatchMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        for col in CompetitorProduct.__table__.columns:
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

        self.catalog_ean = Product(
            sku="CAT-EAN",
            name="Phone Alpha",
            ean="5901234123457",
            brand="Alpha",
        )
        self.catalog_fuzzy = Product(
            sku="CAT-FUZ",
            name="Widget Pro Max",
            brand="WidgetCo",
        )
        self.db.add_all([self.catalog_ean, self.catalog_fuzzy])
        self.db.flush()

        self.cp_ean = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=self.cat_a.id,
            url="https://shop.test/p/ean",
            title="Phone Alpha",
            ean="5901234123457",
        )
        self.cp_fuzzy = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=self.cat_a.id,
            url="https://shop.test/p/fuz",
            title="Widget Pro Max Special Edition",
            brand="WidgetCo",
        )
        self.cp_other_cat = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=self.cat_b.id,
            url="https://shop.test/p/tv",
            title="TV Box",
        )
        self.db.add_all([self.cp_ean, self.cp_fuzzy, self.cp_other_cat])
        self.db.flush()

        self.cp_confirmed = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=self.cat_a.id,
            url="https://shop.test/p/conf",
            title="Locked",
        )
        self.db.add(self.cp_confirmed)
        self.db.flush()
        self.db.add(
            ProductMatch(
                product_id=self.catalog_ean.id,
                competitor_product_id=self.cp_confirmed.id,
                match_score=Decimal("100"),
                match_method="ean_exact",
                status="confirmed",
            ),
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_ean_exact_creates_auto_matched(self) -> None:
        outcome = apply_match_for_competitor_product(
            self.db,
            self.cp_ean,
            only_unmatched=True,
            min_score=Decimal("60"),
        )
        self.db.commit()
        self.assertEqual(outcome, "matched")
        row = self.db.query(ProductMatch).filter_by(competitor_product_id=self.cp_ean.id).one()
        self.assertEqual(row.status, "auto_matched")
        self.assertEqual(row.match_score, Decimal("100"))

    def test_medium_score_creates_needs_review(self) -> None:
        outcome = apply_match_for_competitor_product(
            self.db,
            self.cp_fuzzy,
            only_unmatched=True,
            min_score=Decimal("60"),
        )
        self.db.commit()
        self.assertEqual(outcome, "matched")
        row = self.db.query(ProductMatch).filter_by(competitor_product_id=self.cp_fuzzy.id).one()
        self.assertEqual(row.status, "needs_review")
        self.assertGreaterEqual(row.match_score, Decimal("60"))
        self.assertLess(row.match_score, Decimal("95"))

    def test_skips_confirmed(self) -> None:
        outcome = apply_match_for_competitor_product(
            self.db,
            self.cp_confirmed,
            only_unmatched=True,
            min_score=Decimal("60"),
        )
        self.assertEqual(outcome, "confirmed")

    def test_skips_rejected(self) -> None:
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=self.cat_a.id,
            url="https://shop.test/p/rej",
            title="Rejected item",
        )
        self.db.add(cp)
        self.db.flush()
        self.db.add(
            ProductMatch(
                product_id=self.catalog_fuzzy.id,
                competitor_product_id=cp.id,
                match_score=Decimal("50"),
                match_method="token_overlap",
                status="rejected",
            ),
        )
        self.db.commit()
        outcome = apply_match_for_competitor_product(self.db, cp, only_unmatched=True, min_score=Decimal("60"))
        self.assertEqual(outcome, "rejected")

    def test_only_unmatched_skips_existing_match(self) -> None:
        self.db.add(
            ProductMatch(
                product_id=self.catalog_fuzzy.id,
                competitor_product_id=self.cp_fuzzy.id,
                match_score=Decimal("70"),
                match_method="brand_and_fuzzy_name",
                status="needs_review",
            ),
        )
        self.db.commit()
        outcome = apply_match_for_competitor_product(
            self.db,
            self.cp_fuzzy,
            only_unmatched=True,
            min_score=Decimal("60"),
        )
        self.assertEqual(outcome, "already_matched")

    def test_respects_category_id(self) -> None:
        result = run_batch_match_competitor_products(
            self.db,
            competitor_id=self.competitor.id,
            category_id=self.cat_b.id,
            only_unmatched=True,
            min_score=Decimal("60"),
        )
        self.assertEqual(result["total"], 1)
        rows = self.db.query(ProductMatch).filter_by(competitor_product_id=self.cp_other_cat.id).all()
        self.assertEqual(len(rows), 0)


if __name__ == "__main__":
    unittest.main()
