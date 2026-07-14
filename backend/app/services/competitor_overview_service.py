"""Paginated enriched competitor product rows (latest_* on listing + best match)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.db.latest_price import best_match_subquery
from app.db.pagination import clamp_limit, normalize_offset
from app.models import CompetitorProduct, Product
from app.schemas.competitor_overview import CompetitorProductOverview, CompetitorProductOverviewPage
from app.services.listing_price import effective_listing_price, listing_currency, listing_last_scraped_at


def list_competitor_product_overview_page(
    db: Session,
    *,
    limit: int = 75,
    offset: int = 0,
) -> CompetitorProductOverviewPage:
    limit = clamp_limit(limit)
    offset = normalize_offset(offset)

    best = best_match_subquery()
    product_direct = aliased(Product)
    product_match = aliased(Product)

    base = (
        select(
            CompetitorProduct,
            best.c.match_score.label("match_score"),
            best.c.match_method.label("match_method"),
            best.c.status.label("match_status"),
            product_direct.sku.label("direct_sku"),
            product_direct.name.label("direct_name"),
            product_match.sku.label("match_sku"),
            product_match.name.label("match_name"),
        )
        .outerjoin(best, best.c.competitor_product_id == CompetitorProduct.id)
        .outerjoin(product_direct, product_direct.id == CompetitorProduct.product_id)
        .outerjoin(product_match, product_match.id == best.c.product_id)
    )

    total = int(db.scalar(select(func.count()).select_from(CompetitorProduct)) or 0)
    page_stmt = base.order_by(CompetitorProduct.created_at.desc()).limit(limit).offset(offset)
    rows = db.execute(page_stmt).all()

    items: list[CompetitorProductOverview] = []
    for row in rows:
        cp: CompetitorProduct = row[0]
        comp = cp.competitor
        display_sku = row.direct_sku or row.match_sku
        display_name = row.direct_name or row.match_name
        match_status = row.match_status if row.match_status else ("linked" if cp.product_id else None)

        items.append(
            CompetitorProductOverview(
                id=cp.id,
                competitor_id=cp.competitor_id,
                competitor_name=comp.name,
                url=cp.url,
                title=cp.title,
                last_seen_at=listing_last_scraped_at(cp) or cp.last_seen_at,
                latest_price=effective_listing_price(cp),
                currency=listing_currency(cp),
                availability=cp.latest_availability,
                product_id=cp.product_id,
                match_status=match_status,
                match_score=row.match_score,
                match_method=row.match_method,
                matched_sku=display_sku,
                matched_product_name=display_name,
            ),
        )

    return CompetitorProductOverviewPage(
        rows=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(items)) < total,
    )


def list_competitor_product_overview(db: Session) -> list[CompetitorProductOverview]:
    page = list_competitor_product_overview_page(db, limit=75, offset=0)
    return list(page.rows)
