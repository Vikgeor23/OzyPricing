"""Scrape job enqueue endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import competitor_product_service
from app.tasks.scrape_tasks import scrape_competitor_product

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/scrape-product/{competitor_product_id}", status_code=status.HTTP_202_ACCEPTED)
def enqueue_scrape_product(
    competitor_product_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    cp = competitor_product_service.get_competitor_product(db, competitor_product_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="Competitor product not found")
    scrape_competitor_product.delay(str(competitor_product_id))
    return {"status": "queued", "competitor_product_id": str(competitor_product_id)}
