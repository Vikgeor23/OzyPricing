"""Celery tasks for batch competitor product scraping."""

from __future__ import annotations

import logging
import time
import uuid

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models import Competitor
from app.services.scrape_cancel import cancel_requested
from app.services.scrape_run_lock import release as release_run_lock
from app.services.scraping_batch import run_batch_scrape_competitor_products

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="app.tasks.scrape_batch_tasks.scrape_competitor_products_batch")
def scrape_competitor_products_batch(
    self,
    competitor_id: str,
    category_id: str | None = None,
    only_missing: bool = False,
    only_stale: bool = False,
    stale_hours: int = 24,
    limit: int | None = None,
    skip_recent_failures: bool = True,
    recent_failure_hours: int = 24,
) -> dict:
    t0 = time.perf_counter()
    cid = uuid.UUID(competitor_id)
    cat_uuid = uuid.UUID(category_id) if category_id else None

    logger.info(
        "batch_scrape_start competitor_id=%s category_id=%s only_missing=%s only_stale=%s "
        "stale_hours=%s limit=%s",
        competitor_id,
        category_id,
        only_missing,
        only_stale,
        stale_hours,
        limit,
    )

    db = SessionLocal()
    try:
        if db.get(Competitor, cid) is None:
            err = {"error": "missing competitor", "competitor_id": competitor_id}
            logger.error("batch_scrape_failure competitor_id=%s reason=missing", competitor_id)
            return err

        def on_progress(meta: dict) -> None:
            logger.info(
                "batch_scrape_progress competitor_id=%s category_id=%s current=%s total=%s "
                "scraped=%s failed=%s skipped=%s",
                competitor_id,
                category_id,
                meta.get("current"),
                meta.get("total"),
                meta.get("scraped"),
                meta.get("failed"),
                meta.get("skipped"),
            )
            self.update_state(state="PROGRESS", meta={**meta, "heartbeat_at": time.time()})

        stats = run_batch_scrape_competitor_products(
            db,
            competitor_id=cid,
            category_id=cat_uuid,
            only_missing=only_missing,
            only_stale=only_stale,
            stale_hours=stale_hours,
            limit=limit,
            skip_recent_failures=skip_recent_failures,
            recent_failure_hours=recent_failure_hours,
            progress_callback=on_progress,
            cancel_check=lambda: cancel_requested(str(self.request.id)),
        )
        duration_ms = int((time.perf_counter() - t0) * 1000)
        stats["duration_ms"] = duration_ms
        if stats.get("blocked"):
            stats["current_phase"] = "blocked"
            # Surfaced as a red toast by the workspace poll loop.
            stats["error"] = (
                "Сайтът ни блокира (captcha/anti-bot) — скрейпът е спрян автоматично. "
                "Изчакай блокировката да отмине и пусни отново."
            )
        else:
            stats["current_phase"] = "cancelled" if stats.get("cancelled") else "done"

        logger.info(
            "batch_scrape_success competitor_id=%s category_id=%s total=%s scraped=%s "
            "failed=%s skipped=%s duration_ms=%s",
            competitor_id,
            category_id,
            stats.get("total"),
            stats.get("scraped"),
            stats.get("failed"),
            stats.get("skipped"),
            duration_ms,
        )
        return stats
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "batch_scrape_failure competitor_id=%s category_id=%s duration_ms=%s error=%s",
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
        release_run_lock(competitor_id, str(self.request.id))
        db.close()
