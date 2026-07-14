"""CRUD for competitor product listings."""



from __future__ import annotations



import uuid

from urllib.parse import urlparse



from fastapi import HTTPException, status

from sqlalchemy import func, select

from sqlalchemy.orm import Session



from app.db.pagination import clamp_limit, normalize_offset

from app.models import Competitor, CompetitorProduct

from app.schemas.competitor_product import CompetitorProductListPage, CompetitorProductRead

from app.scrapers.sites.technopolis_urls import (

    normalize_technopolis_product_url,

    parse_technopolis_product_url,

)

from app.services.competitor_category_builder import ensure_category_path_for_competitor_product

from app.services.competitor_category_service import refresh_category_product_counts

from app.utils.url_utils import is_technopolis, normalize_domain, normalize_url





def list_competitor_products(db: Session) -> list[CompetitorProduct]:

    stmt = select(CompetitorProduct).order_by(CompetitorProduct.created_at.desc())

    return list(db.scalars(stmt).all())





def list_competitor_products_page(db: Session, *, limit: int = 75, offset: int = 0) -> CompetitorProductListPage:

    limit = clamp_limit(limit)

    offset = normalize_offset(offset)

    total = int(db.scalar(select(func.count()).select_from(CompetitorProduct)) or 0)

    rows = list(

        db.scalars(

            select(CompetitorProduct)

            .order_by(CompetitorProduct.created_at.desc())

            .limit(limit)

            .offset(offset),

        ).all(),

    )

    items = [CompetitorProductRead.model_validate(x) for x in rows]
    return CompetitorProductListPage(
        rows=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(items)) < total,
    )





def get_competitor_product(db: Session, cp_id: uuid.UUID) -> CompetitorProduct | None:

    return db.get(CompetitorProduct, cp_id)





def _url_host(url: str) -> str:

    parsed = urlparse(normalize_url(url))

    return normalize_domain(parsed.netloc or url)





def normalize_competitor_product_url(url: str, *, competitor_domain: str) -> str:

    """Normalize a listing URL for storage (Technopolis-aware when applicable)."""

    raw = url.strip()

    if not raw:

        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="URL is required")



    if is_technopolis(competitor_domain):

        norm = normalize_technopolis_product_url(raw)

        if norm:

            return norm



    cleaned = normalize_url(raw).split("#")[0].strip()

    return cleaned





def validate_url_for_competitor(url: str, competitor: Competitor) -> None:

    """Raise HTTP 422 when URL host does not match the competitor domain."""

    comp_host = normalize_domain(competitor.domain)

    url_host = _url_host(url)

    if not comp_host or not url_host:

        raise HTTPException(

            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,

            detail="Could not determine URL host for domain validation",

        )

    if url_host != comp_host:

        raise HTTPException(

            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,

            detail=f"URL host {url_host!r} does not match competitor domain {comp_host!r}",

        )





def upsert_competitor_product_url(

    db: Session,

    *,

    competitor_id: uuid.UUID,

    url: str,

    product_id: uuid.UUID | None = None,

) -> tuple[CompetitorProduct, bool]:

    """

    Create or return an existing ``CompetitorProduct`` for a normalized URL.



    Links Technopolis fallback category from URL slug when applicable.

    Returns ``(row, created)`` where ``created`` is False for an existing URL match.

    """

    competitor = db.get(Competitor, competitor_id)

    if competitor is None:

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Competitor not found")



    normalized = normalize_competitor_product_url(url, competitor_domain=competitor.domain)

    validate_url_for_competitor(normalized, competitor)



    parsed = parse_technopolis_product_url(normalized)

    fallback_slug = parsed.get("url_category_slug") if parsed else None



    existing = db.scalars(select(CompetitorProduct).where(CompetitorProduct.url == normalized)).first()

    if existing is not None:

        if existing.competitor_id != competitor_id:

            raise HTTPException(

                status_code=status.HTTP_409_CONFLICT,

                detail="This URL is already linked to another competitor",

            )

        if product_id is not None:

            existing.product_id = product_id

        if fallback_slug:

            ensure_category_path_for_competitor_product(db, existing, None, fallback_slug)

        db.commit()

        db.refresh(existing)

        refresh_category_product_counts(db, competitor_id)

        return existing, False



    cp = CompetitorProduct(

        competitor_id=competitor_id,

        product_id=product_id,

        url=normalized,

    )

    db.add(cp)

    db.flush()

    if fallback_slug:

        ensure_category_path_for_competitor_product(db, cp, None, fallback_slug)

    db.commit()

    db.refresh(cp)

    refresh_category_product_counts(db, competitor_id)

    return cp, True





def create_competitor_product(db: Session, data) -> CompetitorProduct:

    """Backward-compatible create — delegates to upsert."""

    cp, _created = upsert_competitor_product_url(

        db,

        competitor_id=data.competitor_id,

        url=data.url,

        product_id=data.product_id,

    )

    return cp





def delete_competitor_product(db: Session, cp: CompetitorProduct) -> None:

    db.delete(cp)

    db.commit()


