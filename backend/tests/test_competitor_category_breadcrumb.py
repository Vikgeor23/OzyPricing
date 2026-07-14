"""Tests for breadcrumb normalization and resilient category path persistence."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Competitor, CompetitorCategory, CompetitorProduct
from app.scrapers.base import ScrapeResult
from app.services.competitor_category_builder import (
    _normalize_breadcrumb_items,
    category_path_names,
    display_category_path,
    ensure_category_path_for_competitor_product,
)
from app.services.scrape_persist import apply_scrape_result_to_listing
from app.services.workspace_query import WorkspaceQueryParams, list_category_workspace_page


class NormalizeBreadcrumbItemsTests(unittest.TestCase):
    def test_dict_items(self) -> None:
        items = _normalize_breadcrumb_items(
            [
                {"name": "TV, Audio", "url": "https://www.technopolis.bg/bg/tv/"},
                {"name": "  ", "url": "https://example.com/x"},
            ],
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "TV, Audio")
        self.assertIn("technopolis.bg", items[0]["url"] or "")

    def test_string_items(self) -> None:
        items = _normalize_breadcrumb_items(["Phones", "Samsung"])
        self.assertEqual([i["name"] for i in items], ["Phones", "Samsung"])
        self.assertIsNone(items[0]["url"])

    def test_mixed_list(self) -> None:
        items = _normalize_breadcrumb_items(
            [
                "Root",
                {"name": "Child", "url": "https://www.technopolis.bg/bg/child/"},
                None,
                42,
                SimpleNamespace(name="Leaf", url="https://www.technopolis.bg/bg/leaf/"),
            ],
        )
        names = [i["name"] for i in items]
        self.assertEqual(names, ["Root", "Child", "Leaf"])

    def test_invalid_items_skipped(self) -> None:
        class Bad:
            pass

        items = _normalize_breadcrumb_items([Bad(), {}, {"name": ""}])
        self.assertEqual(items, [])


class CategoryPathNamesTests(unittest.TestCase):
    def setUp(self) -> None:
        for table in (CompetitorCategory.__table__,):
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

    def test_none_returns_empty(self) -> None:
        self.assertEqual(category_path_names(self.db, None), [])

    def test_single_category(self) -> None:
        leaf = CompetitorCategory(
            competitor_id=self.competitor.id,
            name="Leaf",
            url="https://www.technopolis.bg/bg/leaf/",
            level=0,
        )
        self.db.add(leaf)
        self.db.flush()
        self.assertEqual(category_path_names(self.db, leaf.id), ["Leaf"])

    def test_nested_category_root_to_leaf(self) -> None:
        root = CompetitorCategory(
            competitor_id=self.competitor.id,
            name="Root",
            url="https://www.technopolis.bg/bg/root/",
            level=0,
        )
        self.db.add(root)
        self.db.flush()
        child = CompetitorCategory(
            competitor_id=self.competitor.id,
            parent_id=root.id,
            name="Child",
            url="https://www.technopolis.bg/bg/child/",
            level=1,
        )
        self.db.add(child)
        self.db.flush()
        leaf = CompetitorCategory(
            competitor_id=self.competitor.id,
            parent_id=child.id,
            name="Leaf",
            url="https://www.technopolis.bg/bg/leaf/",
            level=2,
        )
        self.db.add(leaf)
        self.db.flush()
        self.assertEqual(category_path_names(self.db, leaf.id), ["Root", "Child", "Leaf"])


class EnsureCategoryPathTests(unittest.TestCase):
    def setUp(self) -> None:
        for table in (CompetitorProduct.__table__, CompetitorCategory.__table__):
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
            url="https://www.technopolis.bg/bg/x/p/14251",
        )
        self.db.add(cp)
        self.db.flush()
        return cp

    @patch("app.services.competitor_category_service.refresh_category_product_counts")
    def test_string_breadcrumbs_link_category_when_unassigned(self, mock_refresh) -> None:
        cp = self._cp()
        deepest = ensure_category_path_for_competitor_product(
            self.db,
            cp,
            ["TV, Аудио и Gaming", "TV стойки"],
        )
        self.assertIsNotNone(deepest)
        self.assertEqual(cp.competitor_category_id, deepest.id)
        self.assertEqual(deepest.name, "TV стойки")
        mock_refresh.assert_called_once()

    @patch("app.services.competitor_category_service.refresh_category_product_counts")
    def test_breadcrumbs_do_not_overwrite_discovery_category(self, mock_refresh) -> None:
        discovery = CompetitorCategory(
            competitor_id=self.competitor.id,
            name="Sitemap Category",
            url="https://www.technopolis.bg/bg/sitemap-cat/",
            level=0,
        )
        self.db.add(discovery)
        self.db.flush()
        cp = self._cp()
        cp.competitor_category_id = discovery.id
        self.db.flush()

        breadcrumb_leaf = ensure_category_path_for_competitor_product(
            self.db,
            cp,
            ["TV, Аудио и Gaming", "TV стойки"],
        )

        self.assertIsNotNone(breadcrumb_leaf)
        self.assertNotEqual(breadcrumb_leaf.id, discovery.id)
        self.assertEqual(cp.competitor_category_id, discovery.id)
        mock_refresh.assert_not_called()

    @patch("app.services.competitor_category_service.refresh_category_product_counts")
    def test_raises_never_propagates(self, mock_refresh) -> None:
        cp = self._cp()
        with patch(
            "app.services.competitor_category_builder._get_or_create_category",
            side_effect=RuntimeError("db boom"),
        ):
            result = ensure_category_path_for_competitor_product(
                self.db,
                cp,
                ["Phones"],
                fallback_category_slug="phones",
            )
        self.assertIsNone(result)


class ApplyScrapePersistBreadcrumbTests(unittest.TestCase):
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

    def _success_result(self, *, breadcrumbs: list) -> ScrapeResult:
        captured = datetime.now(timezone.utc)
        return ScrapeResult(
            title="Test Product",
            price=Decimal("19.99"),
            old_price=None,
            promo_price=None,
            currency="EUR",
            availability="in_stock",
            captured_at=captured,
            image_url="https://cdn.example/img.jpg",
            raw_data={
                "scraper_status": "success",
                "scrape_layer": "occ_api",
                "breadcrumb_categories": breadcrumbs,
                "product_identifiers": {"ean": "1234567890123", "brand": "ACME"},
                "specs_json": {"weight": "1kg"},
            },
        )

    @patch("app.services.scrape_persist.get_settings")
    @patch("app.services.competitor_category_service.refresh_category_product_counts")
    def test_persist_succeeds_with_string_breadcrumbs(
        self,
        mock_refresh,
        mock_settings,
    ) -> None:
        mock_settings.return_value.price_history_enabled = False
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/1",
        )
        self.db.add(cp)
        self.db.flush()

        outcome = apply_scrape_result_to_listing(
            self.db,
            cp,
            self._success_result(breadcrumbs=["Cat A", "Cat B"]),
            listing_url=cp.url,
            task_duration_ms=120,
            competitor_product_id=str(cp.id),
        )

        self.assertEqual(outcome, "scraped")
        self.assertEqual(cp.latest_price, Decimal("19.99"))
        self.assertEqual(cp.title, "Test Product")
        self.assertEqual(cp.ean, "1234567890123")
        self.assertEqual(cp.brand, "ACME")
        self.assertIsNotNone(cp.competitor_category_id)

    @patch("app.services.scrape_persist.get_settings")
    def test_persist_succeeds_when_category_builder_raises(self, mock_settings) -> None:
        mock_settings.return_value.price_history_enabled = False
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/2",
        )
        self.db.add(cp)
        self.db.flush()

        with patch(
            "app.services.scrape_persist.ensure_category_path_for_competitor_product",
            side_effect=AttributeError("'str' object has no attribute 'get'"),
        ):
            outcome = apply_scrape_result_to_listing(
                self.db,
                cp,
                self._success_result(breadcrumbs=["Only strings"]),
                listing_url=cp.url,
                task_duration_ms=80,
                competitor_product_id=str(cp.id),
            )

        self.assertEqual(outcome, "scraped")
        self.assertEqual(cp.latest_price, Decimal("19.99"))
        self.assertEqual(cp.latest_scrape_status, "scraped")
        self.assertIsNone(cp.latest_scrape_error)

    @patch("app.services.scrape_persist.get_settings")
    @patch("app.services.competitor_category_service.refresh_category_product_counts")
    def test_scrape_keeps_discovery_category_and_stores_breadcrumbs(
        self,
        mock_refresh,
        mock_settings,
    ) -> None:
        mock_settings.return_value.price_history_enabled = False
        discovery = CompetitorCategory(
            competitor_id=self.competitor.id,
            name="Discovery Cat",
            url="https://www.technopolis.bg/bg/discovery/",
            level=0,
        )
        self.db.add(discovery)
        self.db.flush()
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=discovery.id,
            url="https://www.technopolis.bg/bg/x/p/99",
        )
        self.db.add(cp)
        self.db.flush()

        outcome = apply_scrape_result_to_listing(
            self.db,
            cp,
            self._success_result(breadcrumbs=["OCC Root", "OCC Leaf"]),
            listing_url=cp.url,
            task_duration_ms=50,
            competitor_product_id=str(cp.id),
        )

        self.assertEqual(outcome, "scraped")
        self.assertEqual(cp.competitor_category_id, discovery.id)
        self.assertIsInstance(cp.raw_identifiers, dict)
        self.assertEqual(cp.raw_identifiers.get("breadcrumb_categories"), ["OCC Root", "OCC Leaf"])
        self.assertIsNotNone(cp.raw_identifiers.get("breadcrumb_category_id"))
        mock_refresh.assert_not_called()

    @patch("app.services.competitor_category_service.refresh_category_product_counts")
    def test_category_products_endpoint_after_scrape(self, mock_refresh) -> None:
        discovery = CompetitorCategory(
            competitor_id=self.competitor.id,
            name="Workspace Cat",
            url="https://www.technopolis.bg/bg/workspace/",
            level=0,
        )
        self.db.add(discovery)
        self.db.flush()
        cp = CompetitorProduct(
            competitor_id=self.competitor.id,
            competitor_category_id=discovery.id,
            url="https://www.technopolis.bg/bg/x/p/100",
            title="Before",
        )
        self.db.add(cp)
        self.db.commit()

        ensure_category_path_for_competitor_product(
            self.db,
            cp,
            ["Breadcrumb A", "Breadcrumb B"],
        )
        cp.title = "After scrape"
        ri = dict(cp.raw_identifiers or {})
        ri["breadcrumb_categories"] = ["Breadcrumb A", "Breadcrumb B"]
        cp.raw_identifiers = ri
        self.db.commit()

        page = list_category_workspace_page(
            self.db,
            discovery.id,
            WorkspaceQueryParams(limit=50, offset=0),
        )
        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual(page.total, 1)
        self.assertEqual(page.rows[0].title, "After scrape")
        self.assertEqual(page.rows[0].category_path, ["Breadcrumb A", "Breadcrumb B"])
        self.assertEqual(display_category_path(self.db, cp), ["Breadcrumb A", "Breadcrumb B"])


if __name__ == "__main__":
    unittest.main()
