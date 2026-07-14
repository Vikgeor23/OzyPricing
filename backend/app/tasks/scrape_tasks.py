"""Celery tasks for scraping (placeholder + real site adapters)."""



from __future__ import annotations



import logging

import time

import uuid



from sqlalchemy import select



from app.celery_app import celery_app

from app.database import SessionLocal

from app.models import PriceSnapshot

from app.services.scrape_persist import scrape_competitor_product_by_id



logger = logging.getLogger(__name__)





@celery_app.task(name="app.tasks.scrape_tasks.scrape_competitor_product")

def scrape_competitor_product(competitor_product_id: str) -> str:

    """Run site scraper for the listing URL and persist a `PriceSnapshot`."""

    t_task = time.perf_counter()

    cp_uuid = uuid.UUID(competitor_product_id)

    db = SessionLocal()

    try:

        outcome = scrape_competitor_product_by_id(db, cp_uuid)

        if outcome == "missing_competitor_product":

            logger.error(

                "scraper_failure competitor_product_id=%s url=— duration_ms=0 reason=missing_listing",

                competitor_product_id,

            )

            return "missing_competitor_product"



        latest = db.scalars(

            select(PriceSnapshot.id)

            .where(PriceSnapshot.competitor_product_id == cp_uuid)

            .order_by(PriceSnapshot.captured_at.desc())

            .limit(1),

        ).first()

        return str(latest) if latest else ("scrape_failed" if outcome == "failed" else "scraped")

    except Exception as exc:

        duration_ms = int((time.perf_counter() - t_task) * 1000)

        logger.exception(

            "scraper_failure competitor_product_id=%s duration_ms=%s error=%s layer=db_or_commit",

            competitor_product_id,

            duration_ms,

            exc,

        )

        try:

            db.rollback()

        except Exception:

            logger.exception("rollback failed after scrape task error")

        return "task_error"

    finally:

        db.close()

