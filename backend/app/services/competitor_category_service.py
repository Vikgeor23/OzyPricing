"""Persistence + workspace helpers for `CompetitorCategory`."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import CompetitorCategory, CompetitorProduct, PriceSnapshot, Product, ProductMatch
from app.schemas.category_workspace import CategoryWorkspaceProduct
from app.scrapers.sites.technopolis_categories import CategoryNode
from app.scrapers.sites.technopolis_urls import (
    normalize_technopolis_product_url,
    parse_technopolis_product_url,
)
from app.services.competitor_category_builder import (
    display_category_path,
    ensure_category_path_for_competitor_product,
)
from app.services.workspace_match_fields import parse_match_warnings, parse_top_candidates


def get_category(db: Session, category_id: uuid.UUID) -> CompetitorCategory | None:
    return db.get(CompetitorCategory, category_id)


def replace_category_tree(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    nodes: list[CategoryNode],
) -> None:
    """Replace all categories for a competitor with a fresh discovery run."""

    db.execute(delete(CompetitorCategory).where(CompetitorCategory.competitor_id == competitor_id))

    sorted_nodes = sorted(nodes, key=lambda n: (len(n.url_key), n.name.lower()))
    key_to_id: dict[str, uuid.UUID] = {}

    for n in sorted_nodes:
        parent_id: uuid.UUID | None = None
        if n.parent_url_key:
            parent_id = key_to_id.get(n.parent_url_key)

        row = CompetitorCategory(
            competitor_id=competitor_id,
            parent_id=parent_id,
            name=n.name,
            url=n.url,
            level=n.level,
            path=n.url_key[:1024] if n.url_key else None,
            product_count=0,
        )
        db.add(row)
        db.flush()
        key_to_id[n.url_key] = row.id

    db.commit()


def refresh_category_product_counts(db: Session, competitor_id: uuid.UUID) -> None:
    cats = list(
        db.scalars(select(CompetitorCategory).where(CompetitorCategory.competitor_id == competitor_id)).all(),
    )
    for c in cats:
        cnt = db.scalar(
            select(func.count())
            .select_from(CompetitorProduct)
            .where(CompetitorProduct.competitor_category_id == c.id),
        ) or 0
        c.product_count = int(cnt)
    db.commit()


def upsert_discovered_products(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID,
    urls: list[str],
) -> tuple[int, int]:
    """Insert competitor product rows for new URLs. Returns (created, skipped)."""
    created = 0
    skipped = 0
    for u in urls:
        parsed = parse_technopolis_product_url(u)
        fallback_slug = parsed.get("url_category_slug") if parsed else None

        existing = db.scalars(select(CompetitorProduct).where(CompetitorProduct.url == u)).first()
        if existing is not None:
            if fallback_slug:
                ensure_category_path_for_competitor_product(db, existing, None, fallback_slug)
            else:
                existing.competitor_category_id = category_id
            skipped += 1
            continue
        cp = CompetitorProduct(
            competitor_id=competitor_id,
            competitor_category_id=category_id if not fallback_slug else None,
            url=u,
        )
        db.add(cp)
        db.flush()
        if fallback_slug:
            ensure_category_path_for_competitor_product(db, cp, None, fallback_slug)
        created += 1
    db.commit()
    refresh_category_product_counts(db, competitor_id)
    return created, skipped


def upsert_competitor_product_urls(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    urls: list[str],
) -> tuple[int, int]:
    """Insert competitor product rows for new URLs (full-domain discovery). Returns (created, skipped)."""
    created = 0
    skipped = 0
    for raw in urls:
        normalized = normalize_technopolis_product_url(raw) or raw.split("#")[0].strip()
        parsed = parse_technopolis_product_url(normalized)
        fallback_slug = parsed.get("url_category_slug") if parsed else None

        existing = db.scalars(select(CompetitorProduct).where(CompetitorProduct.url == normalized)).first()
        if existing is not None:
            if fallback_slug:
                ensure_category_path_for_competitor_product(db, existing, None, fallback_slug)
            skipped += 1
            continue

        cp = CompetitorProduct(
            competitor_id=competitor_id,
            url=normalized,
        )
        db.add(cp)
        db.flush()
        if fallback_slug:
            ensure_category_path_for_competitor_product(db, cp, None, fallback_slug)
        created += 1

    db.commit()
    refresh_category_product_counts(db, competitor_id)
    return created, skipped


def _effective_price(snap: PriceSnapshot | None) -> Decimal | None:
    if snap is None:
        return None
    if snap.promo_price is not None:
        return snap.promo_price
    return snap.price


def _pick_best_match_row(db: Session, cp_id: uuid.UUID) -> ProductMatch | None:
    rows = list(db.scalars(select(ProductMatch).where(ProductMatch.competitor_product_id == cp_id)).all())
    rows = [r for r in rows if r.status != "rejected"]
    if not rows:
        return None
    rows.sort(
        key=lambda r: (
            {"confirmed": 0, "auto_matched": 1, "needs_review": 2}.get(r.status, 3),
            -float(r.match_score),
        ),
    )
    return rows[0]


def _workspace_row(
    db: Session,
    cp: CompetitorProduct,
    *,
    competitor_name: str,
) -> CategoryWorkspaceProduct:
    latest_stmt = (
        select(PriceSnapshot)
        .where(PriceSnapshot.competitor_product_id == cp.id)
        .order_by(PriceSnapshot.captured_at.desc())
        .limit(1)
    )
    snap = db.scalars(latest_stmt).first()

    best = _pick_best_match_row(db, cp.id)
    prod_from_link = db.get(Product, cp.product_id) if cp.product_id else None
    prod_from_match = db.get(Product, best.product_id) if best else None
    display_prod = prod_from_link or prod_from_match

    ms = best.match_score if best else None
    mm = best.match_method if best else None
    st: str | None = best.status if best else None
    if st is None and cp.product_id:
        st = "confirmed"
    if st is None:
        st = "no_candidate"

    cat_path = display_category_path(db, cp)
    raw = cp.raw_identifiers if isinstance(cp.raw_identifiers, dict) else {}
    # Descriptions/free-form attributes are no longer exposed; size only.
    specs = cp.specs_json if isinstance(cp.specs_json, dict) else {}
    size = raw.get("size") or specs.get("size")
    listing_attributes = {"size": str(size)} if size else {}

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
        listing_description=None,
        listing_attributes=listing_attributes,
        category_path=cat_path,
        latest_price=_effective_price(snap),
        currency=(snap.currency if snap else "BGN") or "BGN",
        availability=snap.availability if snap else None,
        offered_by=cp.latest_offered_by,
        delivered_by=cp.latest_delivered_by,
        last_seen_at=cp.last_seen_at,
        matched_sku=display_prod.sku if display_prod else None,
        matched_product_name=display_prod.name if display_prod else None,
        match_score=ms,
        match_method=mm,
        match_status=st,
        matched_by=best.matched_by if best else None,
        match_reason=best.match_reason if best else None,
        match_warnings=parse_match_warnings(best.match_warnings if best else None),
        candidate_count=int(best.candidate_count or 0) if best else 0,
        top_candidates=parse_top_candidates(best.top_candidates if best else None),
    )


def list_category_workspace(db: Session, category_id: uuid.UUID) -> list[CategoryWorkspaceProduct]:
    cat = db.get(CompetitorCategory, category_id)
    if cat is None:
        return []

    cps = list(
        db.scalars(
            select(CompetitorProduct)
            .where(CompetitorProduct.competitor_category_id == category_id)
            .order_by(CompetitorProduct.created_at.desc()),
        ).all(),
    )
    return [_workspace_row(db, cp, competitor_name=cat.competitor.name) for cp in cps]


def list_competitor_workspace(db: Session, competitor_id: uuid.UUID) -> list[CategoryWorkspaceProduct]:
    """All products for a competitor (includes uncategorized rows)."""
    from app.models import Competitor

    comp = db.get(Competitor, competitor_id)
    if comp is None:
        return []

    cps = list(
        db.scalars(
            select(CompetitorProduct)
            .where(CompetitorProduct.competitor_id == competitor_id)
            .order_by(CompetitorProduct.created_at.desc()),
        ).all(),
    )
    return [_workspace_row(db, cp, competitor_name=comp.name) for cp in cps]
