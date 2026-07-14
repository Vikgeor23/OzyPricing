"""Price snapshot and aggregated product pricing responses."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PriceSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    competitor_product_id: uuid.UUID
    price: Decimal | None
    old_price: Decimal | None
    promo_price: Decimal | None
    currency: str
    availability: str | None
    captured_at: datetime
    raw_data: dict[str, Any] | None = None


class ProductPriceRow(BaseModel):
    """Latest competitor context for one listing linked to a product."""

    competitor_product_id: uuid.UUID
    competitor_id: uuid.UUID
    competitor_name: str
    competitor_domain: str
    listing_url: str
    listing_title: str | None
    latest_snapshot: PriceSnapshotRead | None


class ProductPricesResponse(BaseModel):
    product_id: uuid.UUID
    rows: list[ProductPriceRow]
