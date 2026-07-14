"""Catalog upload (import batch) API schema."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class ImportBatchRead(BaseModel):
    id: uuid.UUID
    filename: str
    created_at: datetime
    total_rows: int
    imported_rows: int
    skipped_rows: int
    product_count: int
