"""Competitors + nested category trees for the catalog UI."""

import uuid
from typing import Any

from pydantic import BaseModel, Field


class CategoryTreeNode(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    url: str
    level: int
    product_count: int
    children: list["CategoryTreeNode"] = Field(default_factory=list)


class CompetitorTreeItem(BaseModel):
    id: uuid.UUID
    name: str
    domain: str
    country: str | None = None
    currency: str = "BGN"
    scrape_concurrency_max: int | None = None
    categories: list[CategoryTreeNode] = Field(default_factory=list)


class DiscoverQueued(BaseModel):
    status: str = "queued"
    task_id: str | None = None
    message: str | None = None
    discovered_count: int | None = None


class DiscoveryTaskStatus(BaseModel):
    task_id: str
    state: str
    ready: bool
    current_phase: str | None = None
    current: int = 0
    total: int = 0
    product_urls_found: int = 0
    new_urls_found: int = 0
    created: int = 0
    skipped_existing: int = 0
    categories_updated: int = 0
    sitemap_files_checked: int = 0
    pages_scanned: int = 0
    external_queries_checked: int = 0
    rate_limit_pauses: int = 0
    duration_ms: int | None = None
    errors: list[str] = Field(default_factory=list)
    sample_new_urls: list[str] = Field(default_factory=list)
    sample_existing_urls: list[str] = Field(default_factory=list)
    discovery_methods: list[dict[str, Any]] = Field(default_factory=list)
    probe: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
