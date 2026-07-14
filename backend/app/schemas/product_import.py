"""Schemas for XLSX product import."""

from pydantic import BaseModel, Field


class ProductImportErrorItem(BaseModel):
    row: int = Field(..., description="1-based Excel row number")
    message: str


class ProductImportSummary(BaseModel):
    total_rows: int
    imported_rows: int
    skipped_rows: int
    errors: list[ProductImportErrorItem]
