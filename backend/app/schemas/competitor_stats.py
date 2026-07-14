"""Per-competitor dashboard metrics."""

from datetime import datetime

from pydantic import BaseModel, Field


class DiscoverySourceCount(BaseModel):
    source: str
    count: int


class CompetitorStats(BaseModel):
    competitor_id: str
    total_urls: int = 0
    scraped: int = 0
    with_price: int = 0
    failed: int = 0
    never_scraped: int = 0
    dead_urls: int = 0
    matched: int = 0
    auto_matched: int = 0
    needs_review: int = 0
    low_confidence: int = 0
    coverage_pct: float = 0.0
    last_scraped_at: datetime | None = None
    last_discovered_at: datetime | None = None
    discovery_sources: list[DiscoverySourceCount] = Field(default_factory=list)
    scrape_method: str = "HTTP-first with browser fallback"
