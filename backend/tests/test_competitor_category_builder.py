"""Tests for breadcrumb-driven competitor category paths."""

import unittest

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Competitor, CompetitorCategory, CompetitorProduct
from app.services.competitor_category_builder import (
    category_path_names,
    ensure_category_path_for_competitor_product,
)


class CompetitorCategoryBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        for col in CompetitorProduct.__table__.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()

        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(
            self.engine,
            tables=[
                Competitor.__table__,
                CompetitorCategory.__table__,
                CompetitorProduct.__table__,
            ],
        )
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.competitor = Competitor(name="Technopolis", domain="technopolis.bg", currency="BGN")
        self.db.add(self.competitor)
        self.db.commit()
        self.db.refresh(self.competitor)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_fallback_slug_creates_category_and_links_product(self) -> None:
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/Smartfoni-i-mobilni-telefoni/phone/p/1",
        )
        self.db.add(cp)
        self.db.commit()

        deepest = ensure_category_path_for_competitor_product(
            self.db,
            cp,
            None,
            "Smartfoni-i-mobilni-telefoni",
        )
        self.assertIsNotNone(deepest)
        assert deepest is not None
        self.assertEqual(deepest.name, "Smartfoni I Mobilni Telefoni")
        self.db.refresh(cp)
        self.assertEqual(cp.competitor_category_id, deepest.id)
        self.assertEqual(category_path_names(self.db, cp.competitor_category_id), [deepest.name])

    def test_breadcrumb_path_hierarchy_and_deepest_attachment(self) -> None:
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/y/p/99",
        )
        self.db.add(cp)
        self.db.commit()

        breadcrumbs = [
            {"name": "Телефони", "url": "https://www.technopolis.bg/bg/telefoni/"},
            {"name": "Смартфони", "url": "https://www.technopolis.bg/bg/smartfoni/"},
        ]
        deepest = ensure_category_path_for_competitor_product(self.db, cp, breadcrumbs, None)
        self.assertIsNotNone(deepest)
        assert deepest is not None
        self.assertEqual(deepest.name, "Смартфони")
        self.db.refresh(cp)
        self.assertEqual(cp.competitor_category_id, deepest.id)

        root = self.db.get(CompetitorCategory, deepest.parent_id)
        self.assertIsNotNone(root)
        assert root is not None
        self.assertEqual(root.name, "Телефони")
        self.assertEqual(
            category_path_names(self.db, cp.competitor_category_id),
            ["Телефони", "Смартфони"],
        )

    def test_breadcrumbs_preferred_over_fallback(self) -> None:
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/old-slug/product/p/5",
        )
        self.db.add(cp)
        self.db.commit()

        ensure_category_path_for_competitor_product(
            self.db,
            cp,
            [{"name": "Категория A", "url": "https://www.technopolis.bg/bg/a/"}],
            "old-slug",
        )
        self.db.refresh(cp)
        cat = self.db.get(CompetitorCategory, cp.competitor_category_id)
        self.assertIsNotNone(cat)
        assert cat is not None
        self.assertEqual(cat.name, "Категория A")


if __name__ == "__main__":
    unittest.main()
