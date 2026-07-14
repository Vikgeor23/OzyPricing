"""Competitor product listing REST router."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.competitor_overview import CompetitorProductOverviewPage
from app.schemas.competitor_product import (
    CompetitorProductAddResponse,
    CompetitorProductCreate,
    CompetitorProductListPage,
    CompetitorProductRead,
)
from app.schemas.match import FindMatchesResponse, MatchCandidate
from app.services import competitor_product_service
from app.services import competitor_overview_service
from app.services.matching import rank_products_for_listing
from app.services.matching_catalog import fetch_catalog_candidates_for_listing, iter_catalog_batches
from app.tasks.scrape_tasks import scrape_competitor_product

router = APIRouter(prefix="/competitor-products", tags=["competitor-products"])


def _catalog_for_find_matches(db: Session, cp) -> list:
    prefiltered = fetch_catalog_candidates_for_listing(db, cp)
    if prefiltered is not None:
        return prefiltered
    merged: list = []
    for batch in iter_catalog_batches(db):
        merged.extend(batch)
    return merged


@router.get("/overview", response_model=CompetitorProductOverviewPage)
def list_competitor_products_overview(
    limit: int = Query(default=75, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> CompetitorProductOverviewPage:
    """Enriched listings for the Competitors UI (prices, match hints)."""
    return competitor_overview_service.list_competitor_product_overview_page(
        db,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{competitor_product_id}/find-matches",
    response_model=FindMatchesResponse,
)
def find_matches_for_listing(
    competitor_product_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> FindMatchesResponse:
    """Return top 5 catalog product candidates for manual matching."""
    cp = competitor_product_service.get_competitor_product(db, competitor_product_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="Competitor product not found")

    products = _catalog_for_find_matches(db, cp)
    ranked = rank_products_for_listing(products, cp, limit=5)
    candidates = [
        MatchCandidate(
            product_id=p.id,
            sku=p.sku,
            name=p.name,
            brand=p.brand,
            ean=p.ean,
            manufacturer_code=p.manufacturer_code,
            model=p.model,
            image_url=p.image_url,
            own_price=p.own_price,
            match_score=evaln.score,
            match_method=evaln.method,
            match_reasons=evaln.reasons,
            match_warnings=evaln.warnings,
            suggested_status=evaln.suggested_status,
        )
        for p, evaln in ranked
    ]
    return FindMatchesResponse(competitor_product_id=cp.id, candidates=candidates)


@router.get("", response_model=CompetitorProductListPage)
def list_competitor_products(
    limit: int = Query(default=75, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> CompetitorProductListPage:
    return competitor_product_service.list_competitor_products_page(db, limit=limit, offset=offset)


@router.post("", response_model=CompetitorProductAddResponse)
def create_competitor_product(
    payload: CompetitorProductCreate,
    db: Session = Depends(get_db),
) -> CompetitorProductAddResponse:
    """Add or upsert a single competitor product URL (optional scrape after create)."""
    cp, created = competitor_product_service.upsert_competitor_product_url(
        db,
        competitor_id=payload.competitor_id,
        url=payload.url,
        product_id=payload.product_id,
    )
    scrape_task_id: str | None = None
    if payload.scrape_after_create:
        async_result = scrape_competitor_product.delay(str(cp.id))
        scrape_task_id = str(async_result.id)

    base = CompetitorProductRead.model_validate(cp)
    return CompetitorProductAddResponse(
        **base.model_dump(),
        created=created,
        scrape_task_id=scrape_task_id,
    )


@router.post("/{competitor_product_id}/scrape", status_code=status.HTTP_202_ACCEPTED)
def enqueue_scrape_competitor_product(
    competitor_product_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    cp = competitor_product_service.get_competitor_product(db, competitor_product_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="Competitor product not found")
    async_result = scrape_competitor_product.delay(str(competitor_product_id))
    return {"status": "queued", "task_id": str(async_result.id), "competitor_product_id": str(competitor_product_id)}


@router.get("/{competitor_product_id}", response_model=CompetitorProductRead)
def get_competitor_product(
    competitor_product_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> CompetitorProductRead:
    cp = competitor_product_service.get_competitor_product(db, competitor_product_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="Competitor product not found")
    return CompetitorProductRead.model_validate(cp)


@router.delete("/{competitor_product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_competitor_product(competitor_product_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    cp = competitor_product_service.get_competitor_product(db, competitor_product_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="Competitor product not found")
    competitor_product_service.delete_competitor_product(db, cp)
