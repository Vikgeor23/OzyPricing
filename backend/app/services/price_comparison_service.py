"""Paginated price comparison rows for Products page (batched latest prices)."""



from __future__ import annotations



import uuid

from datetime import datetime

from decimal import Decimal



from sqlalchemy import exists, func, or_, select

from sqlalchemy.orm import Session, joinedload



from app.db.pagination import clamp_limit, normalize_offset

from app.config import get_settings
from app.db.latest_price import best_match_subquery, latest_price_subquery
from app.services.listing_price import competitor_price_from_listing

from app.models import Competitor, CompetitorProduct, Product, ProductMatch

from app.schemas.price_comparison import (
    ComparisonCompetitor,
    CompetitorPriceLine,
    PriceComparisonPage,
    PriceComparisonRow,
    PriceComparisonSummary,
)

# Statuses that count as a real link between our product and a listing.
MATCHED_STATUSES = ("confirmed", "auto_matched")


def _matched_products_filter():
    """Products with a confirmed/auto match or a direct listing link."""
    via_match = exists().where(
        ProductMatch.product_id == Product.id,
        ProductMatch.status.in_(MATCHED_STATUSES),
    )
    via_link = exists().where(CompetitorProduct.product_id == Product.id)
    return or_(via_match, via_link)


def _search_filter(search: str):
    like = f"%{search}%"
    return or_(
        Product.sku.ilike(like),
        Product.name.ilike(like),
        Product.ean.ilike(like),
        Product.brand.ilike(like),
    )


def _has_visible_price_filter():
    """Products with ≥1 linked listing that is priced and not explicitly out of
    stock — rows failing this show nothing useful when out-of-stock listings
    are hidden, so the hide_out_of_stock view drops them entirely."""
    listing_visible = (
        or_(
            CompetitorProduct.latest_promo_price.isnot(None),
            CompetitorProduct.latest_price.isnot(None),
        ),
        or_(
            CompetitorProduct.latest_availability.is_(None),
            CompetitorProduct.latest_availability != "out_of_stock",
        ),
    )
    via_match = exists().where(
        ProductMatch.product_id == Product.id,
        ProductMatch.status.in_(MATCHED_STATUSES),
        ProductMatch.competitor_product_id == CompetitorProduct.id,
        *listing_visible,
    )
    via_link = exists().where(
        CompetitorProduct.product_id == Product.id,
        *listing_visible,
    )
    return or_(via_match, via_link)


def _matched_via_competitor_filter(competitor_id: uuid.UUID):
    """Products linked (confirmed/auto/direct) to a listing of this competitor."""
    via_match = exists().where(
        ProductMatch.product_id == Product.id,
        ProductMatch.status.in_(MATCHED_STATUSES),
        ProductMatch.competitor_product_id == CompetitorProduct.id,
        CompetitorProduct.competitor_id == competitor_id,
    )
    via_link = exists().where(
        CompetitorProduct.product_id == Product.id,
        CompetitorProduct.competitor_id == competitor_id,
    )
    return or_(via_match, via_link)


def list_comparison_facets(db: Session) -> dict[str, list[str]]:
    """Distinct category/brand values over matched products (filter dropdowns)."""
    matched = _matched_products_filter()
    categories = [
        v
        for v in db.scalars(
            select(Product.category).where(matched, Product.category.isnot(None)).distinct().order_by(Product.category),
        ).all()
        if v
    ]
    brands = [
        v
        for v in db.scalars(
            select(Product.brand).where(matched, Product.brand.isnot(None)).distinct().order_by(Product.brand),
        ).all()
        if v
    ]
    return {"categories": categories, "brands": brands}


def list_matched_competitors(db: Session) -> list[ComparisonCompetitor]:
    """Competitors that hold at least one linked listing (matrix columns)."""
    via_match = (
        select(CompetitorProduct.competitor_id)
        .join(ProductMatch, ProductMatch.competitor_product_id == CompetitorProduct.id)
        .where(ProductMatch.status.in_(MATCHED_STATUSES))
    )
    via_link = select(CompetitorProduct.competitor_id).where(CompetitorProduct.product_id.isnot(None))
    ids = via_match.union(via_link).subquery()
    comps = db.scalars(
        select(Competitor).where(Competitor.id.in_(select(ids))).order_by(Competitor.name),
    ).all()
    return [ComparisonCompetitor(id=c.id, name=c.name, domain=c.domain) for c in comps]


def build_price_comparison_summary(db: Session) -> PriceComparisonSummary:
    """Small dashboard totals for the comparison matrix header."""
    matched_products = int(
        db.scalar(select(func.count()).select_from(Product).where(_matched_products_filter())) or 0,
    )
    needs_review = int(
        db.scalar(
            select(func.count())
            .select_from(ProductMatch)
            .where(ProductMatch.status == "needs_review"),
        )
        or 0,
    )
    found_urls = int(db.scalar(select(func.count()).select_from(CompetitorProduct)) or 0)
    tracked_sites = int(
        db.scalar(select(func.count()).select_from(Competitor).where(Competitor.is_active.is_(True))) or 0,
    )
    scraped_urls = int(
        db.scalar(
            select(func.count())
            .select_from(CompetitorProduct)
            .where(CompetitorProduct.latest_scraped_at.isnot(None)),
        )
        or 0,
    )
    return PriceComparisonSummary(
        matched_products=matched_products,
        needs_review=needs_review,
        found_urls=found_urls,
        tracked_sites=tracked_sites,
        scraped_urls=scraped_urls,
    )





def _effective_price_from_row(row) -> Decimal | None:

    if row is None:

        return None

    if row.promo_price is not None:

        return row.promo_price

    return row.price





def _batch_linked_cp_ids(

    db: Session,

    product_ids: list[uuid.UUID],
    *,
    only_matched: bool = False,

) -> dict[uuid.UUID, set[uuid.UUID]]:

    result: dict[uuid.UUID, set[uuid.UUID]] = {pid: set() for pid in product_ids}

    if not product_ids:

        return result



    for pid, cp_id in db.execute(

        select(CompetitorProduct.product_id, CompetitorProduct.id).where(

            CompetitorProduct.product_id.in_(product_ids),

        ),

    ):

        if pid in result:

            result[pid].add(cp_id)



    match_status_filter = (
        ProductMatch.status.in_(MATCHED_STATUSES)
        if only_matched
        else ProductMatch.status != "rejected"
    )
    for pid, cp_id in db.execute(
        select(ProductMatch.product_id, ProductMatch.competitor_product_id).where(
            ProductMatch.product_id.in_(product_ids),
            match_status_filter,
        ),
    ):
        if pid in result:
            result[pid].add(cp_id)

    return result


def _batch_best_match_by_cp(
    db: Session,
    competitor_product_ids: set[uuid.UUID] | list[uuid.UUID],
) -> dict[uuid.UUID, object]:
    """Best ProductMatch row per listing (same ranking as workspace)."""
    if not competitor_product_ids:
        return {}
    best = best_match_subquery()
    rows = db.execute(
        select(best).where(best.c.competitor_product_id.in_(list(competitor_product_ids))),
    ).all()
    return {r.competitor_product_id: r for r in rows}





def _compute_status(

    own: Decimal | None,

    lowest: Decimal | None,

    has_links: bool,

    lines: list[CompetitorPriceLine],

) -> str:

    if own is None:

        return "no_own_price"

    if not has_links:

        return "no_competitor_match"

    priced = [ln for ln in lines if ln.price is not None]

    if not priced:

        return "no_recent_price"

    if lowest is None:

        return "no_recent_price"

    if own <= lowest:

        return "cheapest"

    return "more_expensive"





def build_price_comparison_page(

    db: Session,

    *,

    limit: int = 75,

    offset: int = 0,
    search: str | None = None,
    only_matched: bool = False,
    category: str | None = None,
    brand: str | None = None,
    competitor_id: uuid.UUID | None = None,
    hide_out_of_stock: bool = False,

) -> PriceComparisonPage:

    limit = clamp_limit(limit)

    offset = normalize_offset(offset)

    filters = []
    if only_matched:
        filters.append(_matched_products_filter())
    if search and search.strip():
        filters.append(_search_filter(search.strip()))
    if category:
        filters.append(Product.category == category)
    if brand:
        filters.append(Product.brand == brand)
    if competitor_id is not None:
        filters.append(_matched_via_competitor_filter(competitor_id))
    if hide_out_of_stock:
        filters.append(_has_visible_price_filter())

    total = int(
        db.scalar(select(func.count()).select_from(Product).where(*filters)) or 0,
    )

    products = list(

        db.scalars(

            select(Product).where(*filters).order_by(Product.name).limit(limit).offset(offset),

        ).all(),

    )

    competitors = list_matched_competitors(db) if only_matched else []

    if not products:

        return PriceComparisonPage(
            rows=[], total=total, limit=limit, offset=offset, has_more=False, competitors=competitors,
        )



    product_ids = [p.id for p in products]

    links = _batch_linked_cp_ids(db, product_ids, only_matched=only_matched)

    all_cp_ids: set[uuid.UUID] = set()

    for pid in product_ids:

        all_cp_ids |= links[pid]



    cp_by_id: dict[uuid.UUID, CompetitorProduct] = {}

    latest_by_cp: dict[uuid.UUID, object] = {}
    match_by_cp: dict[uuid.UUID, object] = {}

    if all_cp_ids:

        cp_by_id = {

            cp.id: cp

            for cp in db.scalars(

                select(CompetitorProduct)

                .where(CompetitorProduct.id.in_(all_cp_ids))

                .options(joinedload(CompetitorProduct.competitor)),

            ).all()

        }

        if get_settings().price_history_enabled:
            latest = latest_price_subquery()
            for row in db.execute(
                select(latest).where(latest.c.competitor_product_id.in_(list(all_cp_ids))),
            ).all():
                latest_by_cp[row.competitor_product_id] = row

        match_by_cp = _batch_best_match_by_cp(db, all_cp_ids)

    rows: list[PriceComparisonRow] = []

    for product in products:

        cp_ids = links[product.id]

        lines: list[CompetitorPriceLine] = []

        lowest: Decimal | None = None

        last_checked: datetime | None = None



        for cp_id in cp_ids:

            cp = cp_by_id.get(cp_id)

            if cp is None:

                continue

            comp: Competitor = cp.competitor

            snap_row = latest_by_cp.get(cp_id)
            eff, currency, availability, checked = competitor_price_from_listing(
                cp,
                snap_row=snap_row,
            )

            # Explicitly out-of-stock listings are irrelevant for price
            # comparison (unknown availability stays — most listings have none).
            if hide_out_of_stock and availability == "out_of_stock":
                continue

            if checked is not None and (last_checked is None or checked > last_checked):
                last_checked = checked

            match_row = match_by_cp.get(cp_id)
            link_status = None
            if match_row is not None:
                link_status = match_row.status
            elif cp.product_id is not None:
                link_status = "confirmed"

            lines.append(
                CompetitorPriceLine(
                    competitor_id=comp.id,
                    competitor_name=comp.name,
                    domain=comp.domain,
                    price=eff,
                    currency=currency,
                    availability=availability,
                    competitor_product_id=cp.id,
                    title=cp.title,
                    url=cp.url,
                    latest_price=cp.latest_price,
                    latest_promo_price=cp.latest_promo_price,
                    latest_old_price=cp.latest_old_price,
                    latest_scraped_at=cp.latest_scraped_at,
                    match_status=link_status,
                    match_score=match_row.match_score if match_row is not None else None,
                    match_method=match_row.match_method if match_row is not None else None,
                ),
            )

            if eff is not None and (lowest is None or eff < lowest):

                lowest = eff



        diff_pct: Decimal | None = None

        if product.own_price is not None and lowest is not None and product.own_price != 0:

            diff_pct = (product.own_price - lowest) / product.own_price * Decimal("100")



        status = _compute_status(product.own_price, lowest, bool(cp_ids), lines)



        rows.append(

            PriceComparisonRow(

                product_id=product.id,

                sku=product.sku,

                ean=product.ean,

                brand=product.brand,

                name=product.name,

                category=product.category,

                manufacturer_code=product.manufacturer_code,

                own_price=product.own_price,

                competitor_prices=lines,

                lowest_competitor_price=lowest,

                difference_percent=diff_pct,

                last_checked_at=last_checked,

                status=status,

            ),

        )



    return PriceComparisonPage(
        rows=rows,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(rows)) < total,
        competitors=competitors,
    )





def build_price_comparison(db: Session) -> list[PriceComparisonRow]:

    """Backward-compatible: first page only (prefer paginated endpoint)."""

    page = build_price_comparison_page(db, limit=75, offset=0)

    return list(page.rows)

