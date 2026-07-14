"""Tests for batch match outcomes and progress meta."""

from __future__ import annotations

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import JSON, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Competitor, CompetitorProduct, Product, ProductMatch
from app.schemas.match_batch import match_task_status_from_meta
from app.services.match_outcomes import classify_ranked_candidates
from app.services.matching import MatchEvaluation
from app.services.matching_batch import apply_match_for_competitor_product, run_batch_match_competitor_products


def _eval(score: str, method: str) -> MatchEvaluation:
    return MatchEvaluation(
        score=Decimal(score),
        method=method,
        reasons=[f"reason-{method}"],
        warnings=[],
        suggested_status="needs_review",
    )


def _mock_product(label: str = "a") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        sku=f"SKU-{label}",
        name=f"Product {label}",
        brand="Brand",
        ean=None,
        manufacturer_code=None,
        model=None,
        own_price=None,
    )


class MatchOutcomeClassificationTests(unittest.TestCase):
    def test_multiple_close_candidates_needs_review(self) -> None:
        ranked = [
            (_mock_product("a"), _eval("88", "brand_and_fuzzy_name")),
            (_mock_product("b"), _eval("86", "title_similarity")),
        ]
        plan = classify_ranked_candidates(ranked, min_score=Decimal("60"))  # type: ignore[arg-type]
        self.assertEqual(plan.status, "needs_review")
        self.assertEqual(plan.matched_by, "multiple_candidates")
        self.assertEqual(plan.candidate_count, 2)

    def test_low_confidence_keeps_best_score(self) -> None:
        plan = classify_ranked_candidates(
            [(_mock_product(), _eval("45", "token_overlap"))],
            min_score=Decimal("60"),
        )  # type: ignore[arg-type]
        self.assertEqual(plan.status, "low_confidence")
        self.assertEqual(plan.score, Decimal("45"))
        self.assertTrue(plan.persist)
        self.assertEqual(plan.match_reason, "Low confidence match (score 45)")

    def test_no_candidate_when_empty(self) -> None:
        plan = classify_ranked_candidates([], min_score=Decimal("60"))
        self.assertEqual(plan.status, "no_candidate")
        self.assertFalse(plan.persist)


class MatchBatchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        for table in (CompetitorProduct.__table__, ProductMatch.__table__, Product.__table__):
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
                Product.__table__,
                CompetitorProduct.__table__,
                ProductMatch.__table__,
            ],
        )
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.db = self.Session()
        self.competitor = Competitor(name="T", domain="technopolis.bg", currency="BGN")
        self.db.add(self.competitor)
        self.db.flush()
        self.catalog = Product(sku="SKU-1", name="Phone X", ean="1234567890123", brand="Acme")
        self.db.add(self.catalog)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()

    def test_confirmed_skipped_when_only_unmatched(self) -> None:
        cp = CompetitorProduct(competitor_id=self.competitor.id, url="https://www.technopolis.bg/bg/x/p/1")
        self.db.add(cp)
        self.db.flush()
        self.db.add(
            ProductMatch(
                product_id=self.catalog.id,
                competitor_product_id=cp.id,
                match_score=Decimal("100"),
                match_method="ean_exact",
                status="confirmed",
            ),
        )
        self.db.commit()

        outcome, skip_key = apply_match_for_competitor_product(
            self.db,
            cp,
            only_unmatched=True,
            min_score=Decimal("60"),
        )
        self.assertEqual(outcome, "skipped")
        self.assertEqual(skip_key, "already_confirmed")

    @patch("app.services.matching_batch._rank_listing_candidates")
    def test_batch_progress_meta(self, mock_rank) -> None:
        cp_skip = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/skip",
        )
        cp_match = CompetitorProduct(
            competitor_id=self.competitor.id,
            url="https://www.technopolis.bg/bg/x/p/2",
        )
        self.db.add_all([cp_skip, cp_match])
        self.db.flush()
        self.db.add(
            ProductMatch(
                product_id=self.catalog.id,
                competitor_product_id=cp_skip.id,
                match_score=Decimal("100"),
                match_method="ean_exact",
                status="confirmed",
            ),
        )
        self.db.commit()
        mock_rank.return_value = [(self.catalog, _eval("96", "ean_exact"))]

        progress: list[dict] = []

        def on_progress(meta: dict) -> None:
            progress.append(meta)

        result = run_batch_match_competitor_products(
            self.db,
            competitor_id=self.competitor.id,
            only_unmatched=True,
            min_score=Decimal("60"),
            progress_callback=on_progress,
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["needs_review"], 0)
        self.assertIn("skipped_by_reason", result)
        self.assertEqual(result["skipped_by_reason"].get("already_confirmed"), 1)
        self.assertTrue(progress)
        self.assertIn("products_per_minute", progress[-1])

        status = match_task_status_from_meta("t1", "PROGRESS", False, progress[-1])
        self.assertEqual(status.matched, 1)
        self.assertEqual(status.skipped, 1)
        self.assertEqual(status.skipped_by_reason.get("already_confirmed"), 1)


class WorkspaceMatchFieldsTests(unittest.TestCase):
    def test_match_task_status_normalizes_no_candidate(self) -> None:
        status = match_task_status_from_meta(
            "id",
            "SUCCESS",
            True,
            {"no_candidate": 3, "matched": 1, "needs_review": 2, "skipped_by_reason": {"already_confirmed": 1}},
        )
        self.assertEqual(status.no_candidate, 3)
        self.assertEqual(status.no_match, 3)
        self.assertEqual(status.needs_review, 2)


if __name__ == "__main__":
    unittest.main()
