"""Scrape failure classification codes for competitor listings."""

from __future__ import annotations

from typing import Any

# Stored on competitor_products.latest_scrape_error_code
SCRAPE_ERROR_HTTP_BLOCKED = "http_blocked"
SCRAPE_ERROR_HTTP_PARSE_FAILED = "http_parse_failed"
SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT = "playwright_timeout"
SCRAPE_ERROR_PRODUCT_NOT_FOUND = "product_not_found"
SCRAPE_ERROR_PRICE_NOT_FOUND = "price_not_found"
SCRAPE_ERROR_RATE_LIMITED = "rate_limited"
SCRAPE_ERROR_NETWORK = "network_error"
SCRAPE_ERROR_UNKNOWN = "unknown"

HARD_FAIL_SKIP_CODES: frozenset[str] = frozenset(
    {
        SCRAPE_ERROR_PRODUCT_NOT_FOUND,
        SCRAPE_ERROR_PRICE_NOT_FOUND,
        SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT,
    },
)


def classify_scrape_failure(
    *,
    exc: BaseException | None = None,
    error_message: str | None = None,
    http_status: int | None = None,
    raw_data: dict[str, Any] | None = None,
) -> str:
    """Map exception/message/context to a stable error code."""
    raw = raw_data or {}
    msg = (error_message or raw.get("error") or (str(exc) if exc else "") or "").lower()
    err_type = (raw.get("error_type") or (type(exc).__name__ if exc else "")).lower()

    if raw.get("scrape_error_code"):
        return str(raw["scrape_error_code"])

    if "price_missing" in msg:
        return SCRAPE_ERROR_PRICE_NOT_FOUND

    if http_status == 404 or "status_404" in msg or " 404" in msg or "not found" in msg:
        return SCRAPE_ERROR_PRODUCT_NOT_FOUND

    if http_status == 429 or "status_429" in msg or "too many requests" in msg or "rate_limited" in msg:
        return SCRAPE_ERROR_RATE_LIMITED

    if raw.get("http_parse_error") or "http_parse" in msg:
        return SCRAPE_ERROR_HTTP_PARSE_FAILED

    if raw.get("http_fetch_failed") or raw.get("http_blocked") or "http_blocked" in msg:
        if http_status and http_status >= 400:
            return SCRAPE_ERROR_PRODUCT_NOT_FOUND if http_status == 404 else SCRAPE_ERROR_HTTP_BLOCKED
        return SCRAPE_ERROR_HTTP_BLOCKED

    if "timeout" in msg or "timeout" in err_type or err_type == "timeouterror":
        return SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT

    if err_type in ("connecterror", "connecttimeout", "readtimeout", "networkerror"):
        return SCRAPE_ERROR_NETWORK
    if "connection" in msg or "network" in msg:
        return SCRAPE_ERROR_NETWORK

    if "playwrighterror" in err_type or "playwright" in msg:
        return SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT

    return SCRAPE_ERROR_UNKNOWN
