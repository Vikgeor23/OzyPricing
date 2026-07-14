"""Product ↔ competitor listing match API."""

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class MatchCandidate(BaseModel):
    product_id: uuid.UUID
    sku: str
    name: str
    brand: str | None
    ean: str | None
    manufacturer_code: str | None
    model: str | None = None
    image_url: str | None = None
    own_price: Decimal | None = None
    match_score: Decimal
    match_method: str
    match_reasons: list[str] = Field(default_factory=list)
    match_warnings: list[str] = Field(default_factory=list)
    suggested_status: str = "no_match"


class FindMatchesResponse(BaseModel):
    competitor_product_id: uuid.UUID
    candidates: list[MatchCandidate]


class MatchConfirmBody(BaseModel):
    product_id: uuid.UUID
    competitor_product_id: uuid.UUID
    match_score: Decimal
    match_method: str = Field(..., max_length=64)


class MatchRejectBody(BaseModel):
    product_id: uuid.UUID
    competitor_product_id: uuid.UUID
    reason: str | None = Field(None, max_length=512)
