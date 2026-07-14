"""URL health / dead-listing tracking for batch scrape hygiene."""

from __future__ import annotations

from app.models import CompetitorProduct
from app.services.scrape_errors import (
    SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT,
    SCRAPE_ERROR_PRICE_NOT_FOUND,
    SCRAPE_ERROR_PRODUCT_NOT_FOUND,
)

TIMEOUT_STREAK_DEAD = 3
NOT_FOUND_STREAK_DEAD = 2


def update_url_health_after_scrape(
    cp: CompetitorProduct,
    *,
    outcome: str,
    error_code: str | None,
) -> bool:
    """
    Update streak counters and ``is_dead``. Returns True if listing was marked dead.
    """
    if outcome == "scraped":
        cp.consecutive_timeout_count = 0
        cp.consecutive_not_found_count = 0
        return False

    marked_dead = False
    code = error_code or ""

    if code == SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT:
        cp.consecutive_timeout_count = (cp.consecutive_timeout_count or 0) + 1
        cp.consecutive_not_found_count = 0
        if cp.consecutive_timeout_count >= TIMEOUT_STREAK_DEAD:
            cp.is_dead = True
            marked_dead = True
    elif code == SCRAPE_ERROR_PRODUCT_NOT_FOUND:
        cp.consecutive_not_found_count = (cp.consecutive_not_found_count or 0) + 1
        cp.consecutive_timeout_count = 0
        if cp.consecutive_not_found_count >= NOT_FOUND_STREAK_DEAD:
            cp.is_dead = True
            marked_dead = True
    elif code == SCRAPE_ERROR_PRICE_NOT_FOUND:
        cp.consecutive_timeout_count = 0

    return marked_dead
