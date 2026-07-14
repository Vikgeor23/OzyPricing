"""Workspace rows when viewing products under a competitor category."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, computed_field

from app.schemas.match import MatchCandidate


class CategoryWorkspaceProduct(BaseModel):
    competitor_product_id: uuid.UUID
    competitor_category_id: uuid.UUID | None = None
    competitor_name: str
    category_path: list[str] = Field(default_factory=list)
    image_url: str | None = None
    title: str | None = None
    url: str
    listing_ean: str | None = None
    listing_manufacturer_code: str | None = None
    listing_model: str | None = None
    listing_brand: str | None = None
    listing_sku: str | None = None
    listing_shop_code: str | None = None
    listing_extra_code: str | None = None
    listing_size: str | None = None
    listing_color: str | None = None
    listing_description: str | None = None
    listing_attributes: dict[str, Any] = Field(default_factory=dict)
    latest_price: Decimal | None = None
    regular_price: Decimal | None = None
    matched_own_price: Decimal | None = None
    promo_price: Decimal | None = None
    old_price: Decimal | None = None
    currency: str = "BGN"
    availability: str | None = None
    offered_by: str | None = None
    delivered_by: str | None = None
    last_seen_at: datetime | None = None
    last_checked_at: datetime | None = Field(
        None,
        description="Latest snapshot captured_at or listing last_seen_at",
    )
    matched_sku: str | None = None
    matched_product_name: str | None = None
    matched_product_id: uuid.UUID | None = None
    match_score: Decimal | None = None
    match_method: str | None = None
    match_status: str | None = None
    matched_by: str | None = None
    match_reason: str | None = None
    match_warnings: list[str] = Field(default_factory=list)
    candidate_count: int = 0
    top_candidates: list[MatchCandidate] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> uuid.UUID:
        """Alias for competitor_product_id (workspace row id)."""
        return self.competitor_product_id
