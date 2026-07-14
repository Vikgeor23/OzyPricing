"""Enriched competitor listing row for the Competitors module UI."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class CompetitorProductOverviewPage(BaseModel):
    rows: list["CompetitorProductOverview"] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    has_more: bool


class CompetitorProductOverview(BaseModel):
    id: uuid.UUID
    competitor_id: uuid.UUID
    competitor_name: str
    url: str
    title: str | None
    last_seen_at: datetime | None

    latest_price: Decimal | None = None
    currency: str = "BGN"
    availability: str | None = None

    product_id: uuid.UUID | None = None
    match_status: str | None = None
    match_score: Decimal | None = None
    match_method: str | None = None
    matched_sku: str | None = None
    matched_product_name: str | None = None
