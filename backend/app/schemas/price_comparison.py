"""Price comparison table API (Products module)."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ComparisonCompetitor(BaseModel):
    id: uuid.UUID
    name: str
    domain: str


class CompetitorPriceLine(BaseModel):
    competitor_id: uuid.UUID | None = None
    competitor_name: str
    domain: str
    price: Decimal | None
    currency: str = "BGN"
    availability: str | None = None
    competitor_product_id: uuid.UUID
    title: str | None = None
    url: str | None = None
    latest_price: Decimal | None = None
    latest_promo_price: Decimal | None = None
    latest_old_price: Decimal | None = None
    latest_scraped_at: datetime | None = None
    match_status: str | None = None
    match_score: Decimal | None = None
    match_method: str | None = None


class PriceComparisonPage(BaseModel):
    rows: list["PriceComparisonRow"] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    has_more: bool
    # Competitors with ≥1 linked listing — the matrix columns (only_matched view).
    competitors: list[ComparisonCompetitor] = Field(default_factory=list)


class PriceComparisonSummary(BaseModel):
    matched_products: int
    needs_review: int
    found_urls: int
    tracked_sites: int
    scraped_urls: int


class PriceComparisonRow(BaseModel):
    product_id: uuid.UUID
    sku: str
    ean: str | None
    brand: str | None
    name: str
    category: str | None
    manufacturer_code: str | None
    own_price: Decimal | None
    competitor_prices: list[CompetitorPriceLine] = Field(default_factory=list)
    lowest_competitor_price: Decimal | None = None
    difference_percent: Decimal | None = None
    last_checked_at: datetime | None = None
    status: str = Field(
        ...,
        description="cheapest | more_expensive | no_competitor_match | no_recent_price | no_own_price",
    )
