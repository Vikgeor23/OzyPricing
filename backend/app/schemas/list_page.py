"""Generic paginated API responses."""

from pydantic import BaseModel, Field


class ListPage(BaseModel):
    """Paginated list wrapper used by catalog and comparison endpoints."""

    rows: list = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    has_more: bool
