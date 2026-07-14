"""Persist a single competitor listing scrape (shared by Celery tasks)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CompetitorProduct, PriceSnapshot
from app.scrapers.base import ScrapeResult
from app.scrapers.sites.technopolis_playwright_pool import TechnopolisPlaywrightPool
from app.services.scrape_errors import classify_scrape_failure
from app.services.scrape_fetch import fetch_scrape_result_for_listing
from app.services.url_health import update_url_health_after_scrape
from app.services.competitor_category_builder import ensure_category_path_for_competitor_product
from app.utils.url_utils import is_technopolis, normalize_url
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# Price columns are Numeric(14, 4); anything at or above this is either a
# parse artifact (e.g. digits of several prices glued together) or garbage,
# and would raise NumericValueOutOfRange at commit time.
_MAX_PRICE = Decimal("10000000000")


def _sane_price(value: Decimal | None) -> Decimal | None:
    if value is None or abs(value) < _MAX_PRICE:
        return value
    return None


def _update_latest_scrape_fields(
    cp: CompetitorProduct,
    result: ScrapeResult,
    *,
    failed: bool,
) -> None:
    cp.latest_price = result.price
    cp.latest_old_price = result.old_price
    cp.latest_promo_price = result.promo_price
    cp.latest_currency = result.currency or "BGN"
    cp.latest_availability = result.availability
    if not failed:
        offered = result.raw_data.get("offered_by")
        delivered = result.raw_data.get("delivered_by")
        cp.latest_offered_by = str(offered)[:255] if offered else None
        cp.latest_delivered_by = str(delivered)[:255] if delivered else None
    cp.latest_scraped_at = result.captured_at
    cp.latest_scrape_status = "failed" if failed else "scraped"
    if failed:
        err = result.raw_data.get("error")
        cp.latest_scrape_error = str(err) if err is not None else "scrape_failed"
        cp.latest_scrape_error_code = classify_scrape_failure(
            error_message=cp.latest_scrape_error,
            raw_data=result.raw_data,
        )
    else:
        cp.latest_scrape_error = None
        cp.latest_scrape_error_code = None


def _store_breadcrumb_metadata(
    cp: CompetitorProduct,
    merged_raw: dict,
    *,
    breadcrumb_category_id: uuid.UUID | None,
) -> None:
    """Persist breadcrumb path for workspace display without changing discovery category."""
    if breadcrumb_category_id is not None:
        merged_raw["breadcrumb_category_id"] = str(breadcrumb_category_id)
    crumbs = merged_raw.get("breadcrumb_categories")
    if not crumbs and breadcrumb_category_id is None:
        return
    ri = dict(cp.raw_identifiers or {})
    if crumbs:
        ri["breadcrumb_categories"] = crumbs
    if breadcrumb_category_id is not None:
        ri["breadcrumb_category_id"] = str(breadcrumb_category_id)
    cp.raw_identifiers = ri


def _try_update_category_path(
    db: Session,
    cp: CompetitorProduct,
    merged_raw: dict,
    *,
    listing_url: str,
    competitor_product_id: str,
) -> None:
    """Best-effort category path; never raises."""
    if not is_technopolis(listing_url):
        return
    breadcrumbs = merged_raw.get("breadcrumb_categories")
    fallback_slug = merged_raw.get("url_category_slug")
    if not breadcrumbs and not fallback_slug:
        return
    try:
        deepest = ensure_category_path_for_competitor_product(
            db,
            cp,
            breadcrumbs if breadcrumbs else None,
            fallback_slug if not breadcrumbs else None,
        )
        _store_breadcrumb_metadata(
            cp,
            merged_raw,
            breadcrumb_category_id=deepest.id if deepest is not None else None,
        )
    except Exception as exc:  # noqa: BLE001
        merged_raw["category_path_update_failed"] = str(exc)
        logger.info(
            "category_path_update_failed competitor_product_id=%s error=%s",
            competitor_product_id,
            exc,
        )


def apply_scrape_result_to_listing(
    db: Session,
    cp: CompetitorProduct,
    result: ScrapeResult,
    *,
    listing_url: str,
    task_duration_ms: int,
    competitor_product_id: str,
) -> str:
    """
    Update listing latest_* fields and optionally append PriceSnapshot. Does not commit.

    Returns ``scraped`` or ``failed``.
    """
    failed = result.raw_data.get("scraper_status") == "failure"
    if result.price is not None and _sane_price(result.price) is None:
        failed = True
        result.raw_data["scraper_status"] = "failure"
        result.raw_data.setdefault("error", f"price_out_of_range:{result.price}")
        logger.warning(
            "scrape_price_out_of_range competitor_product_id=%s url=%s price=%s",
            competitor_product_id,
            listing_url,
            result.price,
        )
    result.price = _sane_price(result.price)
    result.old_price = _sane_price(result.old_price)
    result.promo_price = _sane_price(result.promo_price)
    merged_raw = {
        **result.raw_data,
        "task_duration_ms": task_duration_ms,
        "competitor_product_id": competitor_product_id,
        "listing_url": listing_url,
    }

    _update_latest_scrape_fields(cp, result, failed=failed)
    cp.last_seen_at = result.captured_at

    if not failed:
        if result.title is not None:
            cp.title = result.title
        if result.image_url:
            cp.image_url = result.image_url

        product_ids = merged_raw.get("product_identifiers") or {}
        if product_ids.get("ean"):
            cp.ean = product_ids["ean"]
        if product_ids.get("manufacturer_code"):
            cp.manufacturer_code = product_ids["manufacturer_code"]
        if product_ids.get("model"):
            cp.model = product_ids["model"]
        if product_ids.get("brand"):
            cp.brand = product_ids["brand"]
        if product_ids.get("sku"):
            cp.sku = product_ids["sku"]
        if product_ids.get("shop_code"):
            cp.shop_code = product_ids["shop_code"]
        if product_ids.get("extra_code"):
            cp.extra_code = product_ids["extra_code"]
        if merged_raw.get("specs_json"):
            cp.specs_json = merged_raw["specs_json"]
        if merged_raw.get("raw_identifiers"):
            cp.raw_identifiers = merged_raw["raw_identifiers"]

        _try_update_category_path(
            db,
            cp,
            merged_raw,
            listing_url=listing_url,
            competitor_product_id=competitor_product_id,
        )

        _expand_variant_siblings(db, cp, result)

    if get_settings().price_history_enabled:
        db.add(
            PriceSnapshot(
                competitor_product_id=cp.id,
                price=result.price,
                old_price=result.old_price,
                promo_price=result.promo_price,
                currency=result.currency,
                availability=result.availability,
                captured_at=result.captured_at,
                raw_data=merged_raw,
            ),
        )

    if not failed:
        update_url_health_after_scrape(cp, outcome="scraped", error_code=None)
        return "scraped"
    update_url_health_after_scrape(
        cp,
        outcome="failed",
        error_code=cp.latest_scrape_error_code,
    )
    return "failed"


def _apply_variant_to_sibling(
    db: Session,
    cp: CompetitorProduct,
    variant: dict,
    parent_cp: CompetitorProduct,
    result: ScrapeResult,
) -> None:
    """Write a size variant's price + identity onto its own listing row."""
    price = _sane_price(variant.get("price"))
    regular = _sane_price(variant.get("regular"))
    promo = None
    old = None
    if regular is not None and price is not None and price < regular:
        old = regular
        promo = price
        display_price = regular
    else:
        display_price = price

    cp.latest_price = display_price
    cp.latest_promo_price = promo
    cp.latest_old_price = old
    cp.latest_currency = variant.get("currency") or result.currency or parent_cp.latest_currency or "EUR"
    cp.latest_availability = result.availability
    cp.latest_scraped_at = result.captured_at
    cp.latest_scrape_status = "scraped"
    cp.latest_scrape_error = None
    cp.latest_scrape_error_code = None
    cp.last_seen_at = result.captured_at

    if variant.get("title"):
        cp.title = variant["title"]
    if not cp.brand and parent_cp.brand:
        cp.brand = parent_cp.brand
    if variant.get("ean"):
        cp.ean = variant["ean"]
    if variant.get("manufacturer_code"):
        cp.manufacturer_code = variant["manufacturer_code"]
    if variant.get("shop_code"):
        cp.shop_code = variant["shop_code"]
        cp.sku = variant["shop_code"]
    if variant.get("size"):
        cp.specs_json = {"size": variant["size"]}
        cp.raw_identifiers = {
            "size": variant["size"],
            "attributes": {"size": variant["size"]},
            "product_code": variant.get("shop_code"),
        }
    if not cp.image_url and parent_cp.image_url:
        cp.image_url = parent_cp.image_url

    update_url_health_after_scrape(cp, outcome="scraped", error_code=None)

    if get_settings().price_history_enabled:
        db.add(
            PriceSnapshot(
                competitor_product_id=cp.id,
                price=display_price,
                old_price=old,
                promo_price=promo,
                currency=cp.latest_currency,
                availability=result.availability,
                captured_at=result.captured_at,
                raw_data={
                    "parse_mode": "variant_expansion",
                    "parent_competitor_product_id": str(parent_cp.id),
                    "size": variant.get("size"),
                    "product_identifiers": {
                        "ean": variant.get("ean"),
                        "manufacturer_code": variant.get("manufacturer_code"),
                        "sku": variant.get("shop_code"),
                        "shop_code": variant.get("shop_code"),
                    },
                },
            ),
        )


def _expand_variant_siblings(
    db: Session,
    parent_cp: CompetitorProduct,
    result: ScrapeResult,
) -> None:
    """Materialise a configurable product's size variants as sibling rows.

    The variant matching the scraped URL is already reflected on ``parent_cp``;
    every other size gets its own row (created on first sight, updated after).
    Idempotent: re-scraping does not duplicate rows.
    """
    variants = result.variants
    if not variants or not get_settings().scrape_expand_variants:
        return

    parent_url = normalize_url(parent_cp.url)
    wanted = {
        normalize_url(v["url"]): v
        for v in variants
        if v.get("url") and normalize_url(v["url"]) != parent_url
    }
    if not wanted:
        return

    existing = {
        normalize_url(row.url): row
        for row in db.execute(
            select(CompetitorProduct).where(
                CompetitorProduct.competitor_id == parent_cp.competitor_id,
                CompetitorProduct.url.in_(list(wanted.keys())),
            ),
        ).scalars()
    }

    for v_url, variant in wanted.items():
        cp = existing.get(v_url)
        if cp is None:
            cp = CompetitorProduct(
                id=uuid.uuid4(),
                competitor_id=parent_cp.competitor_id,
                competitor_category_id=parent_cp.competitor_category_id,
                url=v_url,
                discovered_at=result.captured_at,
                discovery_source="variant_expansion",
            )
            # Insert inside a savepoint so a concurrent worker that raced us to
            # create the same variant row (uq_competitor_product_url) doesn't
            # abort the whole batch — we fall back to updating the existing row.
            try:
                with db.begin_nested():
                    db.add(cp)
                    db.flush()
            except IntegrityError:
                db.expunge(cp)
                cp = db.execute(
                    select(CompetitorProduct).where(
                        CompetitorProduct.competitor_id == parent_cp.competitor_id,
                        CompetitorProduct.url == v_url,
                    ),
                ).scalar_one_or_none()
                if cp is None:
                    continue
        _apply_variant_to_sibling(db, cp, variant, parent_cp, result)


async def fetch_scrape_result_for_competitor_product(
    cp: CompetitorProduct,
    *,
    pool: TechnopolisPlaywrightPool | None = None,
) -> ScrapeResult:
    """Fetch scrape result for one listing (no DB writes)."""
    domain = cp.competitor.domain if cp.competitor else ""
    return await fetch_scrape_result_for_listing(
        cp.url,
        domain,
        pool=pool,
    )


def scrape_competitor_product_row(
    db: Session,
    cp: CompetitorProduct,
    *,
    pool: TechnopolisPlaywrightPool | None = None,
) -> str:
    """
    Run site scraper for one listing and persist. Does not commit.

    Returns ``scraped``, ``failed``, or ``missing``.
    """
    if cp is None:
        return "missing"

    t0 = time.perf_counter()
    listing_url = cp.url
    cp_id = str(cp.id)

    logger.info("scraper_start competitor_product_id=%s url=%s", cp_id, listing_url)
    result = asyncio.run(fetch_scrape_result_for_competitor_product(cp, pool=pool))

    duration_ms = int((time.perf_counter() - t0) * 1000)
    failed = result.raw_data.get("scraper_status") == "failure"

    if failed:
        logger.error(
            "scraper_failure competitor_product_id=%s url=%s duration_ms=%s price=%s availability=%s error=%s",
            cp_id,
            listing_url,
            duration_ms,
            result.price,
            result.availability,
            result.raw_data.get("error"),
        )
    else:
        logger.info(
            "scraper_success competitor_product_id=%s url=%s duration_ms=%s price=%s availability=%s layer=%s",
            cp_id,
            listing_url,
            duration_ms,
            result.price,
            result.availability,
            result.raw_data.get("scrape_layer"),
        )

    return apply_scrape_result_to_listing(
        db,
        cp,
        result,
        listing_url=listing_url,
        task_duration_ms=duration_ms,
        competitor_product_id=cp_id,
    )


def scrape_competitor_product_by_id(db: Session, competitor_product_id: uuid.UUID) -> str:
    """Load listing, scrape, commit. Used by single-product Celery task."""
    cp = db.get(CompetitorProduct, competitor_product_id)
    if cp is None:
        return "missing_competitor_product"
    try:
        outcome = scrape_competitor_product_row(db, cp)
        db.commit()
        return outcome
    except Exception:
        db.rollback()
        raise
