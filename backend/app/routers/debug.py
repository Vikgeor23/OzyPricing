"""Lightweight runtime verification endpoints (observability)."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter

from app.config import get_settings
from app.schemas.debug_runtime import ScrapeRuntimeDebug
from app.scrapers.sites.technopolis_occ_api import OCC_TEST_PRODUCT_CODE, fetch_occ_product

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])

WORKER_VERSION = "pricing-monitor-backend-phase1-occ-observability"


@router.get("/scrape-runtime", response_model=ScrapeRuntimeDebug)
async def scrape_runtime_debug() -> ScrapeRuntimeDebug:
    """
    Live scrape config + one OCC probe against a known Technopolis product.

    Use from the same process/container as Celery/API to verify API reachability.
    """
    settings = get_settings()
    t0 = time.perf_counter()
    status, payload, err = await fetch_occ_product(OCC_TEST_PRODUCT_CODE)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    price_str: str | None = None
    if isinstance(payload, dict):
        price_obj = payload.get("price")
        if isinstance(price_obj, dict) and price_obj.get("value") is not None:
            price_str = str(price_obj.get("value"))

    logger.info(
        "scrape_runtime_debug occ_test_status=%s occ_test_duration_ms=%s scrape_occ_enabled=%s",
        status,
        duration_ms,
        settings.scrape_occ_enabled,
    )

    return ScrapeRuntimeDebug(
        scrape_occ_enabled=settings.scrape_occ_enabled,
        scrape_http_enabled=settings.scrape_http_enabled,
        playwright_enabled=True,
        worker_version=WORKER_VERSION,
        occ_test_product_code=OCC_TEST_PRODUCT_CODE,
        occ_test_status=status,
        occ_test_duration_ms=duration_ms,
        occ_test_error=err,
        occ_test_price=price_str,
    )
