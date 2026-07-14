"""Batch match-all API schemas."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class MatchAllBody(BaseModel):
    category_id: uuid.UUID | None = None
    only_unmatched: bool = True
    limit: int | None = Field(default=None, ge=1)
    min_score: int = Field(default=60, ge=0, le=100)


class MatchAllQueued(BaseModel):
    status: str = "queued"
    task_id: str
    message: str | None = None


class MatchTaskStatus(BaseModel):
    task_id: str
    state: str
    ready: bool
    current: int = 0
    total: int = 0
    matched: int = 0
    needs_review: int = 0
    low_confidence: int = 0
    no_match: int = 0
    no_candidate: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_ms: int | None = None
    current_phase: str | None = None
    products_per_minute: float = 0.0
    skipped_by_reason: dict[str, int] = Field(default_factory=dict)
    result: dict[str, Any] | None = None


def match_task_status_from_meta(task_id: str, state: str, ready: bool, meta: dict[str, Any]) -> MatchTaskStatus:
    """Normalize Celery PROGRESS/result meta into a stable poll response."""
    skipped_by = meta.get("skipped_by_reason")
    if not isinstance(skipped_by, dict):
        skipped_by = {}

    no_candidate = int(meta.get("no_candidate", meta.get("no_match", 0)) or 0)

    return MatchTaskStatus(
        task_id=task_id,
        state=state,
        ready=ready,
        current=int(meta.get("current", 0) or 0),
        total=int(meta.get("total", 0) or 0),
        matched=int(meta.get("matched", 0) or 0),
        needs_review=int(meta.get("needs_review", 0) or 0),
        low_confidence=int(meta.get("low_confidence", 0) or 0),
        no_match=no_candidate,
        no_candidate=no_candidate,
        skipped=int(meta.get("skipped", 0) or 0),
        failed=int(meta.get("failed", 0) or 0),
        errors=list(meta.get("errors") or []),
        duration_ms=meta.get("duration_ms"),
        current_phase=meta.get("current_phase"),
        products_per_minute=float(meta.get("products_per_minute", 0) or 0),
        skipped_by_reason={str(k): int(v) for k, v in skipped_by.items()},
        result=meta.get("result"),
    )
