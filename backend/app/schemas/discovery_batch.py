"""Full-domain incremental product URL discovery API schemas."""

import uuid
from typing import Any

from pydantic import BaseModel, Field


class DiscoverAllBody(BaseModel):
    only_new: bool = True
    force_rescan: bool = False
    limit: int | None = Field(default=None, ge=1)
    source: str = Field(
        default="sitemap",
        description='Discovery strategy: "sitemap" (explicit methods) or "auto" (probe site, run optimal methods)',
    )
    deep_discovery: bool = False
    discovery_methods: list[str] = Field(default_factory=list)
    seed_terms: list[str] = Field(default_factory=list)
    max_search_queries: int | None = Field(default=None, ge=1, le=500)


class DiscoverAllQueued(BaseModel):
    status: str = "queued"
    task_id: str
    message: str | None = None
