"""Batch scrape-all API schemas."""

import uuid
from typing import Any

from pydantic import BaseModel, Field


class ScrapeAllBody(BaseModel):
    category_id: uuid.UUID | None = None
    only_missing: bool = False
    only_stale: bool = False
    stale_hours: int = Field(default=24, ge=1, le=720)
    limit: int | None = Field(default=None, ge=1)
    skip_recent_failures: bool = True
    recent_failure_hours: int = Field(default=24, ge=1, le=720)


class ScrapeAllQueued(BaseModel):
    status: str = "queued"
    task_id: str
    message: str | None = None


class ScrapeTaskStatus(BaseModel):
    task_id: str
    state: str
    ready: bool
    current: int = 0
    total: int = 0
    scraped: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_ms: int | None = None
    current_phase: str | None = None
    pages_scanned: int = 0
    product_urls_found: int = 0
    catalog_total: int = 0
    pages_total: int = 0
    pages_per_minute: float = 0.0
    occ_api_success: int = 0
    occ_api_failed: int = 0
    avg_occ_ms: int = 0
    js_extract_success: int = 0
    adaptive_fast_success: int = 0
    adaptive_playwright_success: int = 0
    lightweight_success: int = 0
    playwright_fallback: int = 0
    avg_scrape_ms: int = 0
    products_per_minute: float = 0.0
    http_skipped: int = 0
    avg_http_ms: int = 0
    avg_playwright_ms: int = 0
    failed_by_reason: dict[str, int] = Field(default_factory=dict)
    current_concurrency: int = 0
    retry_count: int = 0
    timeout_pct: float = 0.0
    success_pct: float = 0.0
    dead_urls_skipped: int = 0
    result: dict[str, Any] | None = None


def scrape_task_status_from_meta(task_id: str, state: str, ready: bool, meta: dict[str, Any]) -> ScrapeTaskStatus:
    """Build poll response from Celery task meta (progress + final result)."""
    return ScrapeTaskStatus(
        task_id=task_id,
        state=state,
        ready=ready,
        current=int(meta.get("current", 0) or 0),
        total=int(meta.get("total", 0) or 0),
        scraped=int(meta.get("scraped", 0) or 0),
        failed=int(meta.get("failed", 0) or 0),
        skipped=int(meta.get("skipped", 0) or 0),
        errors=list(meta.get("errors") or []),
        duration_ms=meta.get("duration_ms"),
        current_phase=meta.get("current_phase"),
        pages_scanned=int(meta.get("pages_scanned", 0) or 0),
        product_urls_found=int(meta.get("product_urls_found", 0) or 0),
        catalog_total=int(meta.get("catalog_total", 0) or 0),
        pages_total=int(meta.get("pages_total", 0) or 0),
        pages_per_minute=float(meta.get("pages_per_minute", 0) or 0),
        occ_api_success=int(meta.get("occ_api_success", 0) or 0),
        occ_api_failed=int(meta.get("occ_api_failed", 0) or 0),
        avg_occ_ms=int(meta.get("avg_occ_ms", 0) or 0),
        js_extract_success=int(meta.get("js_extract_success", 0) or 0),
        adaptive_fast_success=int(meta.get("adaptive_fast_success", 0) or 0),
        adaptive_playwright_success=int(meta.get("adaptive_playwright_success", 0) or 0),
        lightweight_success=int(meta.get("lightweight_success", 0) or 0),
        playwright_fallback=int(meta.get("playwright_fallback", 0) or 0),
        avg_scrape_ms=int(meta.get("avg_scrape_ms", 0) or 0),
        products_per_minute=float(meta.get("products_per_minute", 0) or 0),
        http_skipped=int(meta.get("http_skipped", 0) or 0),
        avg_http_ms=int(meta.get("avg_http_ms", 0) or 0),
        avg_playwright_ms=int(meta.get("avg_playwright_ms", 0) or 0),
        failed_by_reason=dict(meta.get("failed_by_reason") or {}),
        current_concurrency=int(meta.get("current_concurrency", 0) or 0),
        retry_count=int(meta.get("retry_count", 0) or 0),
        timeout_pct=float(meta.get("timeout_pct", 0) or 0),
        success_pct=float(meta.get("success_pct", 0) or 0),
        dead_urls_skipped=int(meta.get("dead_urls_skipped", 0) or 0),
        result=meta.get("result"),
    )
