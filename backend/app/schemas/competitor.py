"""Pydantic models for Competitor API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.url_utils import normalize_domain


class CompetitorBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    domain: str = Field(..., min_length=1, max_length=255)
    country: str | None = Field(None, max_length=64)
    currency: str = Field(default="BGN", max_length=8)
    is_active: bool = True
    # Per-site adaptive concurrency cap; None uses the global setting.
    scrape_concurrency_max: int | None = Field(None, ge=1, le=256)

    @field_validator("domain")
    @classmethod
    def normalize_domain_field(cls, value: str) -> str:
        normalized = normalize_domain(value)
        if not normalized:
            msg = "Invalid domain or URL"
            raise ValueError(msg)
        return normalized


class CompetitorCreate(CompetitorBase):
    pass


class CompetitorUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    domain: str | None = Field(None, min_length=1, max_length=255)
    country: str | None = Field(None, max_length=64)
    currency: str | None = Field(None, max_length=8)
    is_active: bool | None = None
    scrape_concurrency_max: int | None = Field(None, ge=1, le=256)

    @field_validator("domain")
    @classmethod
    def normalize_domain_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_domain(value)
        if not normalized:
            msg = "Invalid domain or URL"
            raise ValueError(msg)
        return normalized


class CompetitorRead(CompetitorBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
