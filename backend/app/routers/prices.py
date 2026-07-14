"""Price snapshot REST router."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.price import PriceSnapshotRead
from app.services import price_service

router = APIRouter(prefix="/price-snapshots", tags=["prices"])


@router.get("", response_model=list[PriceSnapshotRead])
def list_price_snapshots(
    db: Session = Depends(get_db),
    competitor_product_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PriceSnapshotRead]:
    rows = price_service.list_price_snapshots(
        db,
        competitor_product_id=competitor_product_id,
        limit=limit,
    )
    return [PriceSnapshotRead.model_validate(r) for r in rows]
