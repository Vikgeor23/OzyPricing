"""Celery tasks: catalog discovery, listing harvest, category batch matching."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from sqlalchemy import select

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models import Competitor, CompetitorCategory, CompetitorProduct
from app.utils.url_utils import is_technopolis, normalize_domain, technopolis_category_start_url
from app.scrapers.sites.technopolis_categories import discover_technopolis_category_nodes
from app.scrapers.sites.technopolis_discovery import discover_product_urls_for_category
from app.services.competitor_category_service import (
    refresh_category_product_counts,
    replace_category_tree,
    upsert_discovered_products,
)
from app.scrapers.sites.site_probe import probe_site
from app.services.full_discovery_batch import run_incremental_full_discovery
from app.services.scrape_cancel import cancel_requested
from app.services.matching_batch import apply_best_matches_for_category
from app.tasks.scrape_tasks import scrape_competitor_product

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.discovery_tasks.discover_categories_competitor")
def discover_categories_competitor(competitor_id: str) -> dict:
    t0 = time.perf_counter()
    cid = uuid.UUID(competitor_id)
    logger.info("category_discovery_start competitor_id=%s", competitor_id)
    db = SessionLocal()
    try:
        comp = db.get(Competitor, cid)
        if comp is None:
            logger.error("category_discovery_failure competitor_id=%s reason=missing", competitor_id)
            return {"error": "missing competitor"}

        if not is_technopolis(comp.domain):
            normalized = normalize_domain(comp.domain)
            logger.warning(
                "category_discovery_failure competitor_id=%s reason=unsupported_domain domain=%s normalized=%s",
                competitor_id,
                comp.domain,
                normalized,
            )
            return {"error": "unsupported_domain", "domain": comp.domain}

        start_url = technopolis_category_start_url(comp.domain)
        nodes, diag = asyncio.run(discover_technopolis_category_nodes(start_url=start_url))
        replace_category_tree(db, competitor_id=cid, nodes=nodes)
        refresh_category_product_counts(db, cid)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "category_discovery_success competitor_id=%s duration_ms=%s categories=%s",
            competitor_id,
            duration_ms,
            len(nodes),
        )
        return {
            "competitor_id": competitor_id,
            "categories": len(nodes),
            "duration_ms": duration_ms,
            "diag": diag,
        }
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "category_discovery_failure competitor_id=%s duration_ms=%s error=%s",
            competitor_id,
            duration_ms,
            exc,
        )
        return {"error": str(exc), "duration_ms": duration_ms}
    finally:
        db.close()


@celery_app.task(name="app.tasks.discovery_tasks.discover_products_category")
def discover_products_category(category_id: str) -> dict:
    t0 = time.perf_counter()
    cat_uuid = uuid.UUID(category_id)
    logger.info("product_discovery_start category_id=%s", category_id)
    db = SessionLocal()
    try:
        cat = db.get(CompetitorCategory, cat_uuid)
        if cat is None:
            logger.error("product_discovery_failure category_id=%s reason=missing", category_id)
            return {"error": "missing category"}

        urls, diag = asyncio.run(discover_product_urls_for_category(cat.url))
        created, skipped = upsert_discovered_products(
            db,
            competitor_id=cat.competitor_id,
            category_id=cat.id,
            urls=urls,
        )
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "product_discovery_success category_id=%s duration_ms=%s created=%s skipped=%s",
            category_id,
            duration_ms,
            created,
            skipped,
        )
        return {
            "category_id": category_id,
            "created": created,
            "skipped": skipped,
            "discovered_count": created,
            "duration_ms": duration_ms,
            "diag": diag,
        }
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "product_discovery_failure category_id=%s duration_ms=%s error=%s",
            category_id,
            duration_ms,
            exc,
        )
        return {"error": str(exc), "duration_ms": duration_ms}
    finally:
        db.close()


@celery_app.task(name="app.tasks.discovery_tasks.probe_competitor_site")
def probe_competitor_site(competitor_id: str) -> dict:
    """Cheap site probe: detect platform/sitemap/feeds and rank discovery methods."""
    t0 = time.perf_counter()
    cid = uuid.UUID(competitor_id)
    db = SessionLocal()
    try:
        comp = db.get(Competitor, cid)
        if comp is None:
            return {"error": "missing competitor"}
        domain = comp.domain
    finally:
        db.close()
    try:
        result = asyncio.run(probe_site(domain))
        result["competitor_id"] = competitor_id
        logger.info(
            "site_probe_success competitor_id=%s platform=%s best_method=%s duration_ms=%s",
            competitor_id,
            result.get("platform"),
            result.get("best_method"),
            result.get("duration_ms"),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception("site_probe_failure competitor_id=%s duration_ms=%s", competitor_id, duration_ms)
        return {"error": str(exc), "duration_ms": duration_ms, "competitor_id": competitor_id}


@celery_app.task(
    bind=True,
    name="app.tasks.discovery_tasks.discover_all_product_urls_for_competitor",
)
def discover_all_product_urls_for_competitor(
    self,
    competitor_id: str,
    only_new: bool = True,
    force_rescan: bool = False,
    limit: int | None = None,
    source: str = "sitemap",
    deep_discovery: bool = False,
    seed_terms: list[str] | None = None,
    max_search_queries: int | None = None,
    discovery_methods: list[str] | None = None,
    subdomains: list[str] | None = None,
) -> dict:
    """Discover product URLs incrementally (no price scraping or matching)."""
    t0 = time.perf_counter()
    cid = uuid.UUID(competitor_id)
    logger.info(
        "full_product_discovery_start competitor_id=%s only_new=%s force_rescan=%s source=%s deep=%s",
        competitor_id,
        only_new,
        force_rescan,
        source,
        deep_discovery,
    )

    def progress(meta: dict) -> None:
        self.update_state(state="PROGRESS", meta={**meta, "heartbeat_at": time.time()})

    db = SessionLocal()
    try:
        result = run_incremental_full_discovery(
            db,
            cid,
            only_new=only_new,
            force_rescan=force_rescan,
            limit=limit,
            source=source,
            discovery_source=source,
            deep_discovery=deep_discovery,
            seed_terms=seed_terms or [],
            max_search_queries=max_search_queries,
            discovery_methods=discovery_methods or [],
            subdomains=subdomains or [],
            progress=progress,
            cancel_check=lambda: cancel_requested(str(self.request.id)),
        )
        result["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        result["current_phase"] = "cancelled" if result.get("cancelled") else "completed"
        logger.info(
            "full_product_discovery_success competitor_id=%s found=%s new=%s created=%s skipped=%s",
            competitor_id,
            result.get("product_urls_found"),
            result.get("new_urls_found"),
            result.get("created"),
            result.get("skipped_existing"),
        )
        return result
    except ValueError as exc:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.warning(
            "full_product_discovery_failure competitor_id=%s duration_ms=%s error=%s",
            competitor_id,
            duration_ms,
            exc,
        )
        return {"error": str(exc), "duration_ms": duration_ms, "competitor_id": competitor_id}
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "full_product_discovery_failure competitor_id=%s duration_ms=%s error=%s",
            competitor_id,
            duration_ms,
            exc,
        )
        return {"error": str(exc), "duration_ms": duration_ms, "competitor_id": competitor_id}
    finally:
        db.close()


@celery_app.task(name="app.tasks.discovery_tasks.scrape_prices_category")
def scrape_prices_category(category_id: str) -> dict:
    t0 = time.perf_counter()
    cat_uuid = uuid.UUID(category_id)
    logger.info("scrape_category_start category_id=%s", category_id)
    db = SessionLocal()
    try:
        if db.get(CompetitorCategory, cat_uuid) is None:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            return {"error": "missing category", "duration_ms": duration_ms}

        cp_ids = list(
            db.scalars(
                select(CompetitorProduct.id).where(CompetitorProduct.competitor_category_id == cat_uuid),
            ).all(),
        )
    finally:
        db.close()

    for cp_id in cp_ids:
        scrape_competitor_product.delay(str(cp_id))

    duration_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "scrape_category_success category_id=%s duration_ms=%s queued=%s",
        category_id,
        duration_ms,
        len(cp_ids),
    )
    return {"category_id": category_id, "queued": len(cp_ids), "duration_ms": duration_ms}


@celery_app.task(name="app.tasks.discovery_tasks.find_matches_category")
def find_matches_category(category_id: str) -> dict:
    t0 = time.perf_counter()
    cat_uuid = uuid.UUID(category_id)
    logger.info("matching_start category_id=%s", category_id)
    db = SessionLocal()
    try:
        if db.get(CompetitorCategory, cat_uuid) is None:
            return {"error": "missing category"}

        stats = apply_best_matches_for_category(db, cat_uuid)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "matching_success category_id=%s duration_ms=%s stats=%s",
            category_id,
            duration_ms,
            stats,
        )
        return {**stats, "duration_ms": duration_ms}
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception("matching_failure category_id=%s duration_ms=%s", category_id, duration_ms)
        return {"error": str(exc), "duration_ms": duration_ms}
    finally:
        db.close()
