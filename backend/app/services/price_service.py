"""Price snapshot listing."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PriceSnapshot


def list_price_snapshots(
    db: Session,
    *,
    competitor_product_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[PriceSnapshot]:
    stmt = select(PriceSnapshot).order_by(PriceSnapshot.captured_at.desc())
    if competitor_product_id is not None:
        stmt = stmt.where(PriceSnapshot.competitor_product_id == competitor_product_id)
    stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())
