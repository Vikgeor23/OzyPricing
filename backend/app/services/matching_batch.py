"""Batch-match competitor listings to catalog products (Celery-backed)."""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from typing import Callable, Iterator

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Competitor, CompetitorProduct, Product, ProductMatch
from app.services.match_outcomes import (
    MatchPersistPlan,
    classify_ranked_candidates,
    skip_reason_key,
)
from app.services.matching import MatchEvaluation, rank_products_for_listing
from app.services.matching_index import CatalogIndex
from app.services.matching_catalog import fetch_catalog_candidates_for_listing, iter_catalog_batches

logger = logging.getLogger(__name__)

CP_BATCH_SIZE = 500
PRODUCT_BATCH_SIZE = 500
SKIP_CONFIRMED = "confirmed"
SKIP_REJECTED = "rejected"
SKIP_MATCHED = "already_matched"

RANK_LIMIT = 5
RANK_MIN_SCORE = Decimal("1")


def _matchable_filter(stmt):
    """Only listings worth matching: successfully scraped (have a price) and
    not marked dead — matching unscraped/404 listings only pollutes results."""
    return stmt.where(
        CompetitorProduct.is_dead.is_(False),
        CompetitorProduct.latest_price.isnot(None),
    )


def _count_competitor_products(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None,
) -> int:
    stmt = select(func.count()).select_from(CompetitorProduct).where(
        CompetitorProduct.competitor_id == competitor_id,
    )
    stmt = _matchable_filter(stmt)
    if category_id is not None:
        stmt = stmt.where(CompetitorProduct.competitor_category_id == category_id)
    return int(db.scalar(stmt) or 0)


def _iter_competitor_product_ids(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None,
    limit: int | None,
) -> Iterator[list[uuid.UUID]]:
    offset = 0
    processed = 0
    while True:
        batch_limit = CP_BATCH_SIZE
        if limit is not None:
            remaining = limit - processed
            if remaining <= 0:
                break
            batch_limit = min(batch_limit, remaining)

        stmt = (
            select(CompetitorProduct.id)
            .where(CompetitorProduct.competitor_id == competitor_id)
            .order_by(CompetitorProduct.created_at.desc())
            .offset(offset)
            .limit(batch_limit)
        )
        stmt = _matchable_filter(stmt)
        if category_id is not None:
            stmt = stmt.where(CompetitorProduct.competitor_category_id == category_id)

        ids = list(db.scalars(stmt).all())
        if not ids:
            break
        yield ids
        processed += len(ids)
        offset += len(ids)
        if limit is not None and processed >= limit:
            break


def _should_skip_competitor_product(
    db: Session,
    competitor_product_id: uuid.UUID,
    *,
    only_unmatched: bool,
) -> str | None:
    """Return skip reason or None if processing should continue."""
    rows = list(
        db.scalars(
            select(ProductMatch).where(ProductMatch.competitor_product_id == competitor_product_id),
        ).all(),
    )
    if any(r.status == SKIP_CONFIRMED for r in rows):
        return SKIP_CONFIRMED
    if any(r.status == SKIP_REJECTED for r in rows):
        return SKIP_REJECTED
    if only_unmatched and rows:
        return SKIP_MATCHED
    return None


def _rank_listing_candidates(
    db: Session,
    cp: CompetitorProduct,
    *,
    index: CatalogIndex | None = None,
) -> list[tuple[Product, MatchEvaluation]]:
    """Return up to five scored catalog candidates (weak signals included)."""
    ranked: list[tuple[Product, MatchEvaluation]] = []

    def _consider(products: list[Product]) -> None:
        nonlocal ranked
        batch_ranked = rank_products_for_listing(
            products,
            cp,
            limit=RANK_LIMIT,
            min_score=RANK_MIN_SCORE,
        )
        seen: set[uuid.UUID] = {p.id for p, _ in ranked}
        for product, evaln in batch_ranked:
            if product.id not in seen:
                ranked.append((product, evaln))
                seen.add(product.id)
        ranked.sort(key=lambda x: x[1].score, reverse=True)
        ranked = ranked[:RANK_LIMIT]

    if index is not None:
        _consider(index.candidates_for(cp))
        return ranked

    prefiltered = fetch_catalog_candidates_for_listing(db, cp)
    if prefiltered is not None:
        _consider(prefiltered)
        return ranked

    for products in iter_catalog_batches(db, batch_size=PRODUCT_BATCH_SIZE):
        _consider(products)
    return ranked


def _persist_match_plan(db: Session, *, competitor_product_id: uuid.UUID, plan: MatchPersistPlan) -> None:
    db.execute(
        delete(ProductMatch).where(
            ProductMatch.competitor_product_id == competitor_product_id,
            ProductMatch.status.notin_([SKIP_CONFIRMED, SKIP_REJECTED]),
        ),
    )
    if not plan.persist or plan.product is None:
        return

    db.add(
        ProductMatch(
            product_id=plan.product.id,
            competitor_product_id=competitor_product_id,
            match_score=plan.score,
            match_method=plan.method,
            status=plan.status,
            match_reason=plan.match_reason,
            match_warnings=plan.match_warnings or None,
            candidate_count=plan.candidate_count,
            top_candidates=plan.top_candidates or None,
            matched_by=plan.matched_by,
        ),
    )


def apply_match_for_competitor_product(
    db: Session,
    cp: CompetitorProduct,
    *,
    only_unmatched: bool,
    min_score: Decimal,
    index: CatalogIndex | None = None,
) -> tuple[str, str | None]:
    """
    Match one listing.

    Returns ``(outcome, skip_reason_key)`` where skip_reason_key is set for skipped rows.
    Does not set ``CompetitorProduct.product_id``.
    """
    skip = _should_skip_competitor_product(db, cp.id, only_unmatched=only_unmatched)
    if skip:
        return "skipped", skip_reason_key(skip)

    ranked = _rank_listing_candidates(db, cp, index=index)
    plan = classify_ranked_candidates(ranked, min_score=min_score)
    _persist_match_plan(db, competitor_product_id=cp.id, plan=plan)
    return plan.outcome, None


def _empty_progress_stats() -> dict:
    return {
        "matched": 0,
        "needs_review": 0,
        "low_confidence": 0,
        "no_candidate": 0,
        "no_match": 0,
        "skipped": 0,
        "failed": 0,
        "skipped_by_reason": {},
    }


def run_batch_match_competitor_products(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None = None,
    only_unmatched: bool = True,
    limit: int | None = None,
    min_score: Decimal = Decimal("60"),
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    """
    Match competitor products in batches. Commits after each competitor-product batch.
    """
    total = _count_competitor_products(db, competitor_id=competitor_id, category_id=category_id)
    if limit is not None:
        total = min(total, limit)

    stats = _empty_progress_stats()
    errors: list[str] = []
    current = 0
    wall_t0 = time.perf_counter()
    processed_for_rate = 0

    def _report(phase: str, *, force: bool = False) -> None:
        if not progress_callback:
            return
        wall_ms = int((time.perf_counter() - wall_t0) * 1000)
        ppm = 0.0
        if wall_ms > 0 and processed_for_rate > 0:
            ppm = round(processed_for_rate / (wall_ms / 60_000), 2)
        progress_callback(
            {
                "current": current,
                "total": total,
                "matched": stats["matched"],
                "needs_review": stats["needs_review"],
                "low_confidence": stats["low_confidence"],
                "no_candidate": stats["no_candidate"],
                "no_match": stats["no_candidate"],
                "skipped": stats["skipped"],
                "failed": stats["failed"],
                "skipped_by_reason": dict(stats["skipped_by_reason"]),
                "current_phase": phase,
                "competitor_id": str(competitor_id),
                "category_id": str(category_id) if category_id else None,
                "errors": errors[-20:],
                "products_per_minute": ppm,
            },
        )

    _report("loading_catalog", force=True)
    index = CatalogIndex.load(db)
    _report("matching", force=True)

    for id_batch in _iter_competitor_product_ids(
        db,
        competitor_id=competitor_id,
        category_id=category_id,
        limit=limit,
    ):
        try:
            cps = list(
                db.scalars(select(CompetitorProduct).where(CompetitorProduct.id.in_(id_batch))).all(),
            )
            for cp in cps:
                try:
                    outcome, skip_key = apply_match_for_competitor_product(
                        db,
                        cp,
                        only_unmatched=only_unmatched,
                        min_score=min_score,
                        index=index,
                    )
                    if outcome == "skipped" and skip_key:
                        stats["skipped"] += 1
                        stats["skipped_by_reason"][skip_key] = (
                            stats["skipped_by_reason"].get(skip_key, 0) + 1
                        )
                    elif outcome == "auto_matched":
                        stats["matched"] += 1
                    elif outcome == "needs_review":
                        stats["needs_review"] += 1
                    elif outcome == "low_confidence":
                        stats["low_confidence"] += 1
                    elif outcome == "no_candidate":
                        stats["no_candidate"] += 1
                    else:
                        stats["failed"] += 1
                except Exception as exc:  # noqa: BLE001
                    stats["failed"] += 1
                    errors.append(f"{cp.id}: {exc}")
                    logger.exception("batch_match_row_failure competitor_product_id=%s", cp.id)
                current += 1
                processed_for_rate += 1
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            errors.append(str(exc))
            logger.exception("batch_match_batch_failure competitor_id=%s", competitor_id)
        _report("matching")

    stats["no_match"] = stats["no_candidate"]
    return {
        "competitor_id": str(competitor_id),
        "category_id": str(category_id) if category_id else None,
        "total": total,
        "current": current,
        "errors": errors,
        **stats,
    }


def apply_best_matches_for_category(db: Session, category_id: uuid.UUID) -> dict[str, int]:
    """Legacy sync helper used by category find-matches Celery task."""
    from app.models import CompetitorCategory

    cat = db.get(CompetitorCategory, category_id)
    if cat is None:
        return {"matched_rows": 0, "skipped": 0, "total": 0}

    result = run_batch_match_competitor_products(
        db,
        competitor_id=cat.competitor_id,
        category_id=category_id,
        only_unmatched=False,
        min_score=Decimal("60"),
    )
    return {
        "matched_rows": result["matched"] + result["needs_review"] + result["low_confidence"],
        "skipped": result["skipped"],
        "total": result["total"],
    }
