"""Celery tasks for batch product matching."""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models import Competitor
from app.services.matching_batch import run_batch_match_competitor_products

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="app.tasks.match_tasks.match_competitor_products_batch")
def match_competitor_products_batch(
    self,
    competitor_id: str,
    category_id: str | None = None,
    only_unmatched: bool = True,
    limit: int | None = None,
    min_score: int = 60,
) -> dict:
    t0 = time.perf_counter()
    cid = uuid.UUID(competitor_id)
    cat_uuid = uuid.UUID(category_id) if category_id else None

    logger.info(
        "batch_match_start competitor_id=%s category_id=%s only_unmatched=%s limit=%s min_score=%s",
        competitor_id,
        category_id,
        only_unmatched,
        limit,
        min_score,
    )

    db = SessionLocal()
    try:
        if db.get(Competitor, cid) is None:
            err = {"error": "missing competitor", "competitor_id": competitor_id}
            logger.error("batch_match_failure competitor_id=%s reason=missing", competitor_id)
            return err

        def on_progress(meta: dict) -> None:
            logger.info(
                "batch_match_progress competitor_id=%s category_id=%s current=%s total=%s "
                "matched=%s no_match=%s skipped=%s",
                competitor_id,
                category_id,
                meta.get("current"),
                meta.get("total"),
                meta.get("matched"),
                meta.get("no_match"),
                meta.get("skipped"),
            )
            self.update_state(state="PROGRESS", meta={**meta, "heartbeat_at": time.time()})

        stats = run_batch_match_competitor_products(
            db,
            competitor_id=cid,
            category_id=cat_uuid,
            only_unmatched=only_unmatched,
            limit=limit,
            min_score=Decimal(str(min_score)),
            progress_callback=on_progress,
        )
        duration_ms = int((time.perf_counter() - t0) * 1000)
        stats["duration_ms"] = duration_ms
        stats["current_phase"] = "done"

        logger.info(
            "batch_match_success competitor_id=%s category_id=%s total=%s matched=%s "
            "no_match=%s skipped=%s duration_ms=%s",
            competitor_id,
            category_id,
            stats.get("total"),
            stats.get("matched"),
            stats.get("no_match"),
            stats.get("skipped"),
            duration_ms,
        )
        return stats
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "batch_match_failure competitor_id=%s category_id=%s duration_ms=%s error=%s",
            competitor_id,
            category_id,
            duration_ms,
            exc,
        )
        return {
            "error": str(exc),
            "competitor_id": competitor_id,
            "category_id": category_id,
            "duration_ms": duration_ms,
        }
    finally:
        db.close()
