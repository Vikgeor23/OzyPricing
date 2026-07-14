"""SQL-paginated competitor workspace product queries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, case, func, literal, nullslast, or_, select
from sqlalchemy.orm import Session, aliased

from app.db.latest_price import best_match_subquery
from app.db.pagination import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, clamp_limit as clamp_workspace_limit
from app.models import Competitor, CompetitorCategory, CompetitorProduct, Product, ProductMatch
from app.schemas.category_workspace import CategoryWorkspaceProduct
from app.schemas.workspace_page import CategoryWorkspacePage
from app.services.competitor_category_builder import display_category_path
from app.services.listing_price import effective_listing_price, listing_currency, listing_last_checked_at
from app.services.workspace_match_fields import parse_match_warnings, parse_top_candidates

SORT_LAST_SCRAPED = "last_scraped_at"
SORT_LAST_CHECKED = "last_checked"  # alias for API compat
SORT_CREATED_AT = "created_at"


@dataclass(frozen=True)
class WorkspaceQueryParams:
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0
    search: str | None = None
    status: str | None = None
    has_price: bool | None = None
    scraped: bool | None = None
    sort_by: str = SORT_LAST_SCRAPED
    sort_dir: str = "desc"


def _effective_price_column():
    return func.coalesce(CompetitorProduct.latest_promo_price, CompetitorProduct.latest_price)


def _scraped_expr():
    return or_(
        CompetitorProduct.latest_scraped_at.isnot(None),
        CompetitorProduct.latest_price.isnot(None),
        CompetitorProduct.latest_promo_price.isnot(None),
    )


def _effective_status_expr(best):
    return case(
        (CompetitorProduct.product_id.isnot(None), literal("confirmed")),
        (best.c.status.isnot(None), best.c.status),
        else_=literal("no_candidate"),
    )


def _status_filter_condition(status: str):
    """Sargable equivalent of ``_effective_status_expr(best) == status``.

    The CASE-over-join form forces a full scan of the (up to million-row)
    competitor before ORDER BY/LIMIT. These conditions instead drive off the
    small product_matches set (via best_match_subquery) so the planner starts
    from the matched ids, not every product.
    """
    if status == "confirmed":
        return CompetitorProduct.product_id.isnot(None)

    matched_cp_ids = select(best_match_subquery(include_rejected=True).c.competitor_product_id)
    if status == "no_candidate":
        return and_(
            CompetitorProduct.product_id.is_(None),
            CompetitorProduct.id.notin_(matched_cp_ids),
        )

    best = best_match_subquery(include_rejected=True)
    return and_(
        CompetitorProduct.product_id.is_(None),
        CompetitorProduct.id.in_(
            select(best.c.competitor_product_id).where(best.c.status == status),
        ),
    )


def _normalize_sort_by(sort_by: str) -> str:
    if sort_by in (SORT_LAST_SCRAPED, SORT_LAST_CHECKED, "last_checked"):
        return SORT_LAST_SCRAPED
    if sort_by == SORT_CREATED_AT:
        return SORT_CREATED_AT
    return SORT_LAST_SCRAPED


def _workspace_order_by(params: WorkspaceQueryParams):
    desc = params.sort_dir.lower() != "asc"
    sort_key = _normalize_sort_by(params.sort_by)

    if sort_key == SORT_CREATED_AT:
        col = CompetitorProduct.created_at
        return (col.asc(),) if not desc else (col.desc(),)

    scraped_col = CompetitorProduct.latest_scraped_at
    if desc:
        return (nullslast(scraped_col.desc()), CompetitorProduct.created_at.desc())
    return (nullslast(scraped_col.asc()), CompetitorProduct.created_at.asc())


def _category_path_cache(db: Session, competitor_id: uuid.UUID) -> dict[uuid.UUID, list[str]]:
    cats = list(
        db.scalars(
            select(CompetitorCategory).where(CompetitorCategory.competitor_id == competitor_id),
        ).all(),
    )
    by_id = {c.id: c for c in cats}
    cache: dict[uuid.UUID, list[str]] = {}

    for cat_id in by_id:
        names: list[str] = []
        seen: set[uuid.UUID] = set()
        current = by_id.get(cat_id)
        while current is not None and current.id not in seen:
            seen.add(current.id)
            names.append(current.name)
            current = by_id.get(current.parent_id) if current.parent_id else None
        names.reverse()
        cache[cat_id] = names

    return cache


def _apply_workspace_filters(stmt, *, best, product_direct, product_match, params: WorkspaceQueryParams):
    if params.search:
        pattern = f"%{params.search.strip()}%"
        # Structure the search so the pg_trgm indexes are usable: keep the
        # competitor_products columns as a same-table OR (BitmapOr of cp trgm
        # indexes), and reach the linked/matched product columns through
        # index-driven subqueries instead of the outer joins. An OR that mixes
        # base-table and joined-table columns forces Postgres to join the whole
        # (million-row) set first and filter after — which is what spilled.
        matching_product_ids = select(Product.id).where(
            or_(
                Product.sku.ilike(pattern),
                Product.name.ilike(pattern),
                Product.ean.ilike(pattern),
                Product.brand.ilike(pattern),
                Product.manufacturer_code.ilike(pattern),
                Product.model.ilike(pattern),
            ),
        )
        stmt = stmt.where(
            or_(
                CompetitorProduct.title.ilike(pattern),
                CompetitorProduct.url.ilike(pattern),
                CompetitorProduct.sku.ilike(pattern),
                CompetitorProduct.ean.ilike(pattern),
                CompetitorProduct.brand.ilike(pattern),
                CompetitorProduct.manufacturer_code.ilike(pattern),
                CompetitorProduct.model.ilike(pattern),
                CompetitorProduct.product_id.in_(matching_product_ids),
                CompetitorProduct.id.in_(
                    select(ProductMatch.competitor_product_id).where(
                        ProductMatch.status != "rejected",
                        ProductMatch.product_id.in_(matching_product_ids),
                    ),
                ),
            ),
        )

    if params.status:
        stmt = stmt.where(_status_filter_condition(params.status))

    if params.has_price is True:
        stmt = stmt.where(_effective_price_column().isnot(None))
    elif params.has_price is False:
        stmt = stmt.where(_effective_price_column().is_(None))

    if params.scraped is True:
        stmt = stmt.where(_scraped_expr())
    elif params.scraped is False:
        stmt = stmt.where(~_scraped_expr())

    return stmt


def _build_workspace_select(*, best, product_direct, product_match):
    return (
        select(
            CompetitorProduct,
            best.c.match_score.label("match_score"),
            best.c.match_method.label("match_method"),
            best.c.status.label("match_status_raw"),
            best.c.match_reason.label("match_reason"),
            best.c.match_warnings.label("match_warnings"),
            best.c.candidate_count.label("candidate_count"),
            best.c.top_candidates.label("top_candidates"),
            best.c.matched_by.label("matched_by"),
            product_direct.sku.label("direct_sku"),
            product_direct.name.label("direct_name"),
            product_direct.own_price.label("direct_own_price"),
            product_match.sku.label("match_sku"),
            product_match.name.label("match_name"),
            product_match.own_price.label("match_own_price"),
            best.c.product_id.label("matched_product_id"),
        )
        .outerjoin(best, best.c.competitor_product_id == CompetitorProduct.id)
        .outerjoin(product_direct, product_direct.id == CompetitorProduct.product_id)
        .outerjoin(product_match, product_match.id == best.c.product_id)
    )


def _listing_description(cp: CompetitorProduct) -> str | None:
    # Descriptions are no longer collected or exposed.
    return None


def _listing_attributes(cp: CompetitorProduct) -> dict:
    # Free-form attributes are no longer exposed; only the variant size/color.
    out: dict[str, str] = {}
    size = _listing_size(cp)
    if size:
        out["size"] = size
    color = _listing_color(cp)
    if color:
        out["color"] = color
    return out


def _listing_size(cp: CompetitorProduct) -> str | None:
    return _listing_spec(cp, ("size", "разфасовка", "вместимост", "volume", "capacity"))


def _listing_color(cp: CompetitorProduct) -> str | None:
    return _listing_spec(cp, ("color", "colour", "цвят", "нюанс", "оттенък", "shade"))


def _listing_spec(cp: CompetitorProduct, keys: tuple[str, ...]) -> str | None:
    raw = cp.raw_identifiers if isinstance(cp.raw_identifiers, dict) else {}
    specs = cp.specs_json if isinstance(cp.specs_json, dict) else {}
    for source in (raw, raw.get("attributes"), specs):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value:
                return str(value)
    return None


def _row_to_schema(
    row,
    *,
    competitor_name: str,
    path_cache: dict[uuid.UUID, list[str]],
    db: Session,
) -> CategoryWorkspaceProduct:
    cp: CompetitorProduct = row[0]

    display_sku = row.direct_sku or row.match_sku
    display_name = row.direct_name or row.match_name
    display_own_price = row.direct_own_price if row.direct_sku else row.match_own_price

    match_status: str | None = row.match_status_raw
    if match_status is None and cp.product_id:
        match_status = "confirmed"
    if match_status is None:
        match_status = "no_candidate"

    cat_path = display_category_path(db, cp, assigned_path_cache=path_cache)

    return CategoryWorkspaceProduct(
        competitor_product_id=cp.id,
        competitor_category_id=cp.competitor_category_id,
        competitor_name=competitor_name,
        image_url=cp.image_url,
        title=cp.title,
        url=cp.url,
        listing_ean=cp.ean,
        listing_manufacturer_code=cp.manufacturer_code,
        listing_model=cp.model,
        listing_brand=cp.brand,
        listing_sku=cp.sku,
        listing_shop_code=cp.shop_code,
        listing_extra_code=cp.extra_code,
        listing_size=_listing_size(cp),
        listing_color=_listing_color(cp),
        listing_description=_listing_description(cp),
        listing_attributes=_listing_attributes(cp),
        category_path=cat_path,
        latest_price=effective_listing_price(cp),
        regular_price=cp.latest_price,
        promo_price=cp.latest_promo_price,
        old_price=cp.latest_old_price,
        currency=listing_currency(cp),
        availability=cp.latest_availability,
        offered_by=cp.latest_offered_by,
        delivered_by=cp.latest_delivered_by,
        last_seen_at=cp.last_seen_at,
        last_checked_at=listing_last_checked_at(cp),
        matched_sku=display_sku,
        matched_product_name=display_name,
        matched_own_price=display_own_price,
        matched_product_id=row.matched_product_id,
        match_score=row.match_score,
        match_method=row.match_method,
        match_status=match_status,
        matched_by=row.matched_by,
        match_reason=row.match_reason,
        match_warnings=parse_match_warnings(row.match_warnings),
        candidate_count=int(row.candidate_count or 0),
        top_candidates=parse_top_candidates(row.top_candidates),
    )


def paginate_workspace(
    db: Session,
    *,
    scope_where,
    competitor_id: uuid.UUID,
    competitor_name: str,
    params: WorkspaceQueryParams,
) -> CategoryWorkspacePage:
    limit = clamp_workspace_limit(params.limit)
    offset = max(0, params.offset)

    best = best_match_subquery(include_rejected=True)
    product_direct = aliased(Product)
    product_match = aliased(Product)

    # NB: scope_where is intentionally NOT applied here. The page is hydrated
    # by primary-key IN (page_ids), which already implies the scope; adding
    # the competitor filter on top makes the planner estimate rows=1 and pick
    # a nested loop that re-runs the best-match window subquery once per row
    # (~9s per page on a fresh 23k-listing competitor).
    base = _build_workspace_select(
        best=best,
        product_direct=product_direct,
        product_match=product_match,
    )

    # Always paginate over a lean CompetitorProduct.id query (joining match /
    # product tables only when a filter actually needs them) and hydrate the
    # wide row set for the ~page of ids only. Selecting/sorting/counting the
    # full wide join before OFFSET is what let a filtered search over a
    # million-row competitor sort-spill to disk for minutes.
    # Both status and search filters are expressed as sargable subqueries
    # against the small product_matches/products sets, so the id query needs no
    # join — it stays a lean scan of competitor_products.id.
    id_stmt = select(CompetitorProduct.id).where(scope_where)
    id_stmt = _apply_workspace_filters(
        id_stmt,
        best=best,
        product_direct=product_direct,
        product_match=product_match,
        params=params,
    )

    total = int(db.scalar(select(func.count()).select_from(id_stmt.subquery())) or 0)
    page_ids = list(
        db.scalars(id_stmt.order_by(*_workspace_order_by(params)).limit(limit).offset(offset)).all(),
    )
    if page_ids:
        page_stmt = base.where(CompetitorProduct.id.in_(page_ids)).order_by(
            *_workspace_order_by(params),
        )
        rows = db.execute(page_stmt).all()
    else:
        rows = []

    path_cache = _category_path_cache(db, competitor_id)
    items = [
        _row_to_schema(r, competitor_name=competitor_name, path_cache=path_cache, db=db) for r in rows
    ]

    return CategoryWorkspacePage(
        rows=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(items)) < total,
    )


def list_category_workspace_page(
    db: Session,
    category_id: uuid.UUID,
    params: WorkspaceQueryParams,
) -> CategoryWorkspacePage | None:
    cat = db.get(CompetitorCategory, category_id)
    if cat is None:
        return None

    return paginate_workspace(
        db,
        scope_where=CompetitorProduct.competitor_category_id == category_id,
        competitor_id=cat.competitor_id,
        competitor_name=cat.competitor.name,
        params=params,
    )


def list_competitor_workspace_page(
    db: Session,
    competitor_id: uuid.UUID,
    params: WorkspaceQueryParams,
) -> CategoryWorkspacePage | None:
    comp = db.get(Competitor, competitor_id)
    if comp is None:
        return None

    return paginate_workspace(
        db,
        scope_where=CompetitorProduct.competitor_id == competitor_id,
        competitor_id=competitor_id,
        competitor_name=comp.name,
        params=params,
    )
