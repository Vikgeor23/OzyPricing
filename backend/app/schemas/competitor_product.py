"""Pydantic models for CompetitorProduct API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CompetitorProductBase(BaseModel):
    competitor_id: uuid.UUID
    competitor_category_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    url: str = Field(..., min_length=1)
    title: str | None = None
    brand: str | None = Field(None, max_length=255)
    ean: str | None = Field(None, max_length=64)
    manufacturer_code: str | None = Field(None, max_length=255)
    model: str | None = Field(None, max_length=255)
    sku: str | None = Field(None, max_length=255)
    image_url: str | None = None


class CompetitorProductCreate(CompetitorProductBase):
    scrape_after_create: bool = False

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "competitor_id": "00000000-0000-0000-0000-000000000001",
                "product_id": None,
                "url": "https://www.technopolis.bg/bg/telefoni/item/p/505144",
                "scrape_after_create": False,
            }
        }
    )


class CompetitorProductRead(CompetitorProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CompetitorProductListPage(BaseModel):
    rows: list[CompetitorProductRead] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    has_more: bool


class CompetitorProductAddResponse(CompetitorProductRead):
    created: bool = True
    scrape_task_id: str | None = None
