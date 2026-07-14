"""Reusable SQL subqueries for latest price and best product match per listing."""

from __future__ import annotations

import uuid

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import PriceSnapshot, ProductMatch


def latest_price_subquery():
    """One row per competitor_product_id — latest snapshot by captured_at."""
    ranked = (
        select(
            PriceSnapshot.id,
            PriceSnapshot.competitor_product_id,
            PriceSnapshot.price,
            PriceSnapshot.old_price,
            PriceSnapshot.promo_price,
            PriceSnapshot.currency,
            PriceSnapshot.availability,
            PriceSnapshot.captured_at,
            func.row_number()
            .over(
                partition_by=PriceSnapshot.competitor_product_id,
                order_by=PriceSnapshot.captured_at.desc(),
            )
            .label("rn"),
        )
    ).subquery("ps_ranked")
    return (
        select(
            ranked.c.id,
            ranked.c.competitor_product_id,
            ranked.c.price,
            ranked.c.old_price,
            ranked.c.promo_price,
            ranked.c.currency,
            ranked.c.availability,
            ranked.c.captured_at,
        )
        .where(ranked.c.rn == 1)
        .subquery("latest_price")
    )


def best_match_subquery(*, include_rejected: bool = False, cp_scope=None):
    """One row per competitor_product_id — best non-rejected match by status then score.

    ``cp_scope`` is an optional selectable of competitor_product_ids: when given,
    the ranking window is computed only over matches for those listings instead
    of the whole product_matches table. A page-hydrating caller (workspace) joins
    the subquery to a handful of ids so the global window is cheap, but a caller
    that streams every row of a competitor (export) must scope the window itself
    or it re-ranks millions of unrelated matches on each run.
    """
    status_rank = case(
        (ProductMatch.status == "confirmed", 0),
        (ProductMatch.status == "auto_matched", 1),
        (ProductMatch.status == "needs_review", 2),
        (ProductMatch.status == "low_confidence", 3),
        else_=4,
    )
    base = select(
        ProductMatch.competitor_product_id,
        ProductMatch.product_id,
        ProductMatch.match_score,
        ProductMatch.match_method,
        ProductMatch.status,
        ProductMatch.match_reason,
        ProductMatch.match_warnings,
        ProductMatch.candidate_count,
        ProductMatch.top_candidates,
        ProductMatch.matched_by,
        func.row_number()
        .over(
            partition_by=ProductMatch.competitor_product_id,
            order_by=(status_rank.asc(), ProductMatch.match_score.desc()),
        )
        .label("rn"),
    )
    if not include_rejected:
        base = base.where(ProductMatch.status != "rejected")
    if cp_scope is not None:
        base = base.where(ProductMatch.competitor_product_id.in_(cp_scope))
    ranked = base.subquery("pm_ranked")
    return select(ranked).where(ranked.c.rn == 1).subquery("best_match")


def effective_price_from_latest(latest) -> object:
    return func.coalesce(latest.c.promo_price, latest.c.price)


def load_latest_price_map(
    db: Session,
    competitor_product_ids: set[uuid.UUID] | list[uuid.UUID],
) -> dict[uuid.UUID, object]:
    """Return latest price subquery rows keyed by competitor_product_id."""
    if not competitor_product_ids:
        return {}
    latest = latest_price_subquery()
    rows = db.execute(
        select(latest).where(latest.c.competitor_product_id.in_(list(competitor_product_ids))),
    ).all()
    return {r.competitor_product_id: r for r in rows}
