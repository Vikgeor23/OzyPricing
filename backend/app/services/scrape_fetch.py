"""Fetch scrape results (no DB) — used by single and batch scrape paths."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP

from app.models import CompetitorProduct
from app.scrapers.base import ScrapeResult
from app.scrapers.registry import get_scraper_for_domain
from app.scrapers.sites.technopolis_hybrid import scrape_technopolis_url
from app.scrapers.sites.technopolis_playwright_pool import TechnopolisPlaywrightPool
from app.utils.url_utils import is_technopolis

logger = logging.getLogger(__name__)

EUR_CURRENCY = "EUR"
BGN_PER_EUR = Decimal("1.95583")


def _convert_bgn_to_eur(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return (value / BGN_PER_EUR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalize_result_to_eur(result: ScrapeResult) -> ScrapeResult:
    currency = (result.currency or "").upper()
    if currency == EUR_CURRENCY:
        return result
    if currency != "BGN":
        return result
    raw_data = {
        **result.raw_data,
        "currency_normalized": {
            "from": "BGN",
            "to": EUR_CURRENCY,
            "rate_bgn_per_eur": str(BGN_PER_EUR),
        },
        "original_price": str(result.price) if result.price is not None else None,
        "original_old_price": str(result.old_price) if result.old_price is not None else None,
        "original_promo_price": str(result.promo_price) if result.promo_price is not None else None,
    }
    return replace(
        result,
        price=_convert_bgn_to_eur(result.price),
        old_price=_convert_bgn_to_eur(result.old_price),
        promo_price=_convert_bgn_to_eur(result.promo_price),
        currency=EUR_CURRENCY,
        raw_data=raw_data,
    )


async def fetch_scrape_result_for_listing(
    listing_url: str,
    domain: str,
    *,
    pool: TechnopolisPlaywrightPool | None = None,
    generic_pool: object | None = None,
    competitor_product_id: str | None = None,
) -> ScrapeResult:
    """Run site scraper for one URL without touching the database."""
    t0 = time.perf_counter()
    cp_id = "batch"

    if is_technopolis(listing_url) or is_technopolis(domain):
        try:
            return _normalize_result_to_eur(
                await scrape_technopolis_url(
                    listing_url,
                    pool=pool,
                    competitor_product_id=competitor_product_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.perf_counter() - t0) * 1000)
            logger.exception(
                "scraper_failure competitor_product_id=%s url=%s duration_ms=%s error=%s",
                cp_id,
                listing_url,
                duration_ms,
                exc,
            )
            return ScrapeResult(
                title=None,
                price=None,
                old_price=None,
                promo_price=None,
                currency=EUR_CURRENCY,
                availability=None,
                captured_at=datetime.now(timezone.utc),
                image_url=None,
                raw_data={
                    "scraper_status": "failure",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "duration_ms": duration_ms,
                    "layer": "fetch_scrape_result_for_listing",
                },
            )

    scraper = get_scraper_for_domain(
        domain,
        listing_url,
        preferred_currency=EUR_CURRENCY,
        generic_playwright_pool=generic_pool,
    )
    try:
        return _normalize_result_to_eur(await scraper.run())
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "scraper_failure competitor_product_id=%s url=%s duration_ms=%s error=%s",
            cp_id,
            listing_url,
            duration_ms,
            exc,
        )
        return ScrapeResult(
            title=None,
            price=None,
            old_price=None,
            promo_price=None,
            currency=EUR_CURRENCY,
            availability=None,
            captured_at=datetime.now(timezone.utc),
            image_url=None,
            raw_data={
                "scraper_status": "failure",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "duration_ms": duration_ms,
                "layer": "fetch_scrape_result_for_listing",
            },
        )


def scrape_layer_from_result(result: ScrapeResult) -> str:
    layer = result.raw_data.get("scrape_layer") or result.raw_data.get("fetch_layer")
    if layer in ("http", "playwright", "occ_api"):
        return str(layer)
    return "playwright"
