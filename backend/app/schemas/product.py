"""Pydantic models for Product API."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ProductBase(BaseModel):
    tenant_id: uuid.UUID | None = None
    sku: str = Field(..., min_length=1, max_length=255)
    ean: str | None = Field(None, max_length=64)
    brand: str | None = Field(None, max_length=255)
    name: str = Field(..., min_length=1, max_length=512)
    category: str | None = Field(None, max_length=255)
    manufacturer_code: str | None = Field(None, max_length=255)
    model: str | None = Field(None, max_length=255)
    own_price: Decimal | None = None
    cost_price: Decimal | None = None
    stock_quantity: Decimal | None = None
    product_url: str | None = None
    image_url: str | None = None
    description: str | None = None
    variant: str | None = Field(None, max_length=255)
    color: str | None = Field(None, max_length=255)
    size: str | None = Field(None, max_length=255)
    storage: str | None = Field(None, max_length=128)
    memory: str | None = Field(None, max_length=128)
    supplier_sku: str | None = Field(None, max_length=255)


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    tenant_id: uuid.UUID | None = None
    sku: str | None = Field(None, min_length=1, max_length=255)
    ean: str | None = Field(None, max_length=64)
    brand: str | None = Field(None, max_length=255)
    name: str | None = Field(None, min_length=1, max_length=512)
    category: str | None = Field(None, max_length=255)
    manufacturer_code: str | None = Field(None, max_length=255)
    model: str | None = Field(None, max_length=255)
    own_price: Decimal | None = None
    cost_price: Decimal | None = None
    stock_quantity: Decimal | None = None
    product_url: str | None = None
    image_url: str | None = None
    description: str | None = None
    variant: str | None = Field(None, max_length=255)
    color: str | None = Field(None, max_length=255)
    size: str | None = Field(None, max_length=255)
    storage: str | None = Field(None, max_length=128)
    memory: str | None = Field(None, max_length=128)
    supplier_sku: str | None = Field(None, max_length=255)


class ProductRead(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ProductListPage(BaseModel):
    rows: list[ProductRead] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    has_more: bool
