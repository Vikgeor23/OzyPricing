"""Paginated workspace product listing."""

from pydantic import BaseModel, Field

from app.schemas.category_workspace import CategoryWorkspaceProduct


class CategoryWorkspacePage(BaseModel):
    rows: list[CategoryWorkspaceProduct] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    has_more: bool
