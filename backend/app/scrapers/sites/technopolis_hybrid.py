"""Hybrid Technopolis scrape: optional HTTP first, shared Playwright pool on fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.scrapers.base import ScrapeResult
from app.scrapers.sites.technopolis_js_extract import parse_js_extract_payload
from app.scrapers.sites.technopolis_occ_api import scrape_technopolis_occ
from app.scrapers.sites.technopolis_playwright_pool import PlaywrightFetchResult, TechnopolisPlaywrightPool
from app.services.scrape_errors import (
    SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT,
    SCRAPE_ERROR_PRICE_NOT_FOUND,
    classify_scrape_failure,
)
from app.utils.url_utils import is_technopolis

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_BLOCKED_HTML_MARKERS = (
    "access denied",
    "captcha",
    "cloudflare",
    "please enable javascript",
    "bot detection",
    "cf-browser-verification",
)


def _is_blocked_response(status_code: int, html: str) -> bool:
    if status_code >= 400:
        return True
    if len(html.strip()) < 1200:
        return True
    low = html.lower()
    return any(marker in low for marker in _BLOCKED_HTML_MARKERS)


def _needs_playwright_fallback(result: ScrapeResult, html: str) -> bool:
    if result.raw_data.get("scraper_status") == "failure":
        return True
    if result.price is None:
        return True
    if _is_blocked_response(200, html):
        return True
    return False


def _extract_json_ld_enrichment(soup: BeautifulSoup) -> dict[str, Any]:
    """Pull product fields from JSON-LD when present."""
    out: dict[str, Any] = {}
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = node.get("@type") or ""
            if isinstance(types, list):
                is_product = any("Product" in str(t) for t in types)
            else:
                is_product = "Product" in str(types)
            if not is_product:
                continue
            if node.get("name") and not out.get("title"):
                out["title"] = str(node["name"])[:512]
            if node.get("image"):
                img = node["image"]
                if isinstance(img, list) and img:
                    img = img[0]
                if isinstance(img, dict):
                    img = img.get("url")
                if isinstance(img, str):
                    out["image_url"] = img
            if node.get("gtin13") or node.get("gtin"):
                out["ean"] = str(node.get("gtin13") or node.get("gtin"))
            brand = node.get("brand")
            if isinstance(brand, dict) and brand.get("name"):
                out["brand"] = str(brand["name"])
            elif isinstance(brand, str):
                out["brand"] = brand
            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                if price is not None:
                    try:
                        out["price"] = Decimal(str(price).replace(",", "."))
                    except Exception:  # noqa: BLE001
                        pass
                avail = offers.get("availability") or ""
                if avail:
                    out["availability"] = str(avail).split("/")[-1].lower()
            break
    return out


def _merge_json_ld_into_result(scraper: Any, result: ScrapeResult, html: str) -> ScrapeResult:
    soup = BeautifulSoup(html, "html.parser")
    enrich = _extract_json_ld_enrichment(soup)
    if not enrich:
        return result

    title = result.title or enrich.get("title")
    price = result.price or enrich.get("price")
    image_url = result.image_url or enrich.get("image_url")
    availability = result.availability if result.availability not in (None, "unknown") else enrich.get("availability")

    raw = dict(result.raw_data)
    if enrich.get("ean"):
        ids = dict(raw.get("product_identifiers") or {})
        ids.setdefault("ean", enrich["ean"])
        raw["product_identifiers"] = ids
    if enrich.get("brand"):
        ids = dict(raw.get("product_identifiers") or {})
        ids.setdefault("brand", enrich["brand"])
        raw["product_identifiers"] = ids
    raw["json_ld_enrichment"] = {k: str(v) for k, v in enrich.items() if v is not None}

    return ScrapeResult(
        title=title,
        price=price,
        old_price=result.old_price,
        promo_price=result.promo_price,
        currency=result.currency,
        availability=availability,
        captured_at=result.captured_at,
        image_url=image_url,
        raw_data=raw,
    )


async def fetch_technopolis_html_http(url: str) -> tuple[str | None, int, str | None]:
    """Return (html, status_code, error_message)."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=settings.scrape_http_timeout_sec,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8"},
        ) as client:
            resp = await client.get(url)
            return resp.text, resp.status_code, None
    except Exception as exc:  # noqa: BLE001
        return None, 0, str(exc)


def _success_result(
    scraper: Any,
    result: ScrapeResult,
    *,
    layer: str,
    duration_ms: int,
    started: datetime,
    extra: dict[str, Any],
) -> ScrapeResult:
    merged_raw = {
        **result.raw_data,
        "scraper_status": "success",
        "scrape_layer": layer,
        "duration_ms": duration_ms,
        "scrape_timestamp": started.isoformat(),
        "url": scraper.listing_url,
        **extra,
    }
    return ScrapeResult(
        title=result.title,
        price=result.price,
        old_price=result.old_price,
        promo_price=result.promo_price,
        currency=result.currency,
        availability=result.availability,
        captured_at=result.captured_at,
        image_url=result.image_url,
        raw_data=merged_raw,
    )


async def _fetch_playwright_with_retry(
    pool: TechnopolisPlaywrightPool,
    url: str,
) -> PlaywrightFetchResult:
    """One retry on Playwright timeout with jitter and longer navigation timeout."""
    outcome = await pool.fetch_page_data(url, is_retry=False)
    if not outcome.timed_out:
        return outcome

    await asyncio.sleep(random.uniform(0.5, 2.0))
    retried = await pool.fetch_page_data(url, is_retry=True)
    retried.diagnostics["playwright_retry"] = True
    return retried


def _parse_playwright_fetch(
    scraper: Any,
    fetch: PlaywrightFetchResult,
    *,
    url: str,
    diagnostics: dict[str, Any],
    captured_at: datetime,
) -> ScrapeResult | None:
    """Return ScrapeResult when JS or HTML parse succeeds."""
    merged_diag = {**diagnostics, **fetch.diagnostics}

    if fetch.js_extract:
        js_result = parse_js_extract_payload(fetch.js_extract, url=url, captured_at=captured_at)
        if js_result is not None and js_result.price is not None:
            merged_diag.update(js_result.raw_data)
            return ScrapeResult(
                title=js_result.title,
                price=js_result.price,
                old_price=js_result.old_price,
                promo_price=js_result.promo_price,
                currency=js_result.currency,
                availability=js_result.availability,
                captured_at=captured_at,
                image_url=js_result.image_url,
                raw_data={**merged_diag, **js_result.raw_data},
            )

    if fetch.html:
        parsed = scraper._parse_html_to_result(
            fetch.html,
            extra_raw=merged_diag,
            captured_at=captured_at,
        )
        return _merge_json_ld_into_result(scraper, parsed, fetch.html)
    return None


def _failure_result(
    scraper: Any,
    started: datetime,
    duration_ms: int,
    exc: BaseException,
    *,
    screenshot_path: str | None,
    raw_data: dict[str, Any] | None = None,
) -> ScrapeResult:
    payload: dict[str, Any] = {
        "scraper_status": "failure",
        "error": str(exc),
        "error_type": type(exc).__name__,
        "duration_ms": duration_ms,
        "scrape_timestamp": started.isoformat(),
        "url": scraper.listing_url,
        "extracted_selectors": {},
        "selectors": {},
        **(raw_data or {}),
    }
    payload["scrape_error_code"] = classify_scrape_failure(
        exc=exc,
        error_message=str(exc),
        http_status=payload.get("http_status"),
        raw_data=payload,
    )
    if screenshot_path:
        payload["screenshot_path"] = screenshot_path
    return ScrapeResult(
        title=None,
        price=None,
        old_price=None,
        promo_price=None,
        currency="BGN",
        availability=None,
        captured_at=started,
        image_url=None,
        raw_data=payload,
    )


def _log_scrape_success(
    layer: str,
    *,
    url: str,
    duration_ms: int,
    price: Any,
    competitor_product_id: str | None = None,
) -> None:
    logger.info(
        "scraper_success site=technopolis_bg layer=%s duration_ms=%s "
        "competitor_product_id=%s url=%s price=%s",
        layer,
        duration_ms,
        competitor_product_id or "-",
        url,
        price,
    )


async def scrape_technopolis_url(
    url: str,
    *,
    pool: TechnopolisPlaywrightPool | None = None,
    competitor_product_id: str | None = None,
) -> ScrapeResult:
    """
    Scrape a Technopolis PDP: optional HTTP + parse first; Playwright when required.

    ``pool`` should be provided for batch jobs (one browser for all URLs).
    """
    from app.scrapers.sites.technopolis import TechnopolisScraper

    settings = get_settings()
    t0 = time.perf_counter()
    started = datetime.now(timezone.utc)
    scraper = TechnopolisScraper(url)
    logger.info(
        "scraper_start site=technopolis_bg url=%s hybrid=1 occ_enabled=%s http_enabled=%s",
        url,
        settings.scrape_occ_enabled,
        settings.scrape_http_enabled,
    )

    if not is_technopolis(url):
        ms = int((time.perf_counter() - t0) * 1000)
        return _failure_result(
            scraper,
            started,
            ms,
            ValueError(f"URL host is not technopolis.bg: {url!r}"),
            screenshot_path=None,
        )

    diagnostics: dict[str, Any] = {}
    http_ms = 0

    if settings.scrape_occ_enabled:
        occ_t0 = time.perf_counter()
        occ_result, occ_diag = await scrape_technopolis_occ(
            url,
            competitor_product_id=competitor_product_id,
        )
        occ_ms = int((time.perf_counter() - occ_t0) * 1000)
        diagnostics.update(occ_diag)
        diagnostics["occ_api_duration_ms"] = occ_ms
        if occ_result is not None:
            ms = int((time.perf_counter() - t0) * 1000)
            out = _success_result(
                scraper,
                occ_result,
                layer="occ_api",
                duration_ms=ms,
                started=started,
                extra=diagnostics,
            )
            _log_scrape_success(
                "occ_api",
                url=url,
                duration_ms=ms,
                price=out.price,
                competitor_product_id=competitor_product_id,
            )
            return out
        diagnostics["occ_api_failed"] = True

    if not settings.scrape_http_enabled:
        diagnostics["http_skipped"] = True
    else:
        http_t0 = time.perf_counter()
        html, http_status, http_error = await fetch_technopolis_html_http(url)
        http_ms = int((time.perf_counter() - http_t0) * 1000)
        diagnostics["http_duration_ms"] = http_ms
        diagnostics["http_status"] = http_status

        if html is not None and not _is_blocked_response(http_status, html):
            try:
                parsed = scraper._parse_html_to_result(
                    html,
                    extra_raw={"fetch_layer": "http", "http_status": http_status},
                    captured_at=datetime.now(timezone.utc),
                )
                parsed = _merge_json_ld_into_result(scraper, parsed, html)
                if not _needs_playwright_fallback(parsed, html):
                    ms = int((time.perf_counter() - t0) * 1000)
                    out = _success_result(
                        scraper,
                        parsed,
                        layer="http",
                        duration_ms=ms,
                        started=started,
                        extra={**diagnostics, "playwright_duration_ms": 0},
                    )
                    _log_scrape_success(
                        "http",
                        url=url,
                        duration_ms=ms,
                        price=out.price,
                        competitor_product_id=competitor_product_id,
                    )
                    return out
                diagnostics["http_fallback_reason"] = "missing_price_or_parse"
            except Exception as exc:  # noqa: BLE001
                diagnostics["http_parse_error"] = str(exc)
                logger.debug("technopolis_http_parse_failed url=%s err=%s", url, exc)
        else:
            diagnostics["http_fetch_failed"] = http_error or f"status_{http_status}"
            if http_status >= 400:
                diagnostics["http_status"] = http_status

    pw_t0 = time.perf_counter()
    captured_at = datetime.now(timezone.utc)
    try:
        if pool is not None:
            fetch_outcome = await _fetch_playwright_with_retry(pool, url)
            if fetch_outcome.timed_out:
                pw_ms = int((time.perf_counter() - pw_t0) * 1000)
                diagnostics.update(fetch_outcome.diagnostics)
                diagnostics["scrape_layer"] = "playwright"
                diagnostics["playwright_duration_ms"] = pw_ms
                if http_ms:
                    diagnostics["http_duration_ms"] = http_ms
                ms = int((time.perf_counter() - t0) * 1000)
                return _failure_result(
                    scraper,
                    started,
                    ms,
                    RuntimeError(fetch_outcome.error or "playwright_timeout"),
                    screenshot_path=None,
                    raw_data={
                        **diagnostics,
                        "scrape_error_code": SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT,
                    },
                )
            if fetch_outcome.navigation_failed and not fetch_outcome.html:
                pw_ms = int((time.perf_counter() - pw_t0) * 1000)
                diagnostics.update(fetch_outcome.diagnostics)
                diagnostics["playwright_duration_ms"] = pw_ms
                ms = int((time.perf_counter() - t0) * 1000)
                return _failure_result(
                    scraper,
                    started,
                    ms,
                    RuntimeError(fetch_outcome.error or "playwright_navigation_failed"),
                    screenshot_path=None,
                    raw_data=diagnostics,
                )

            diagnostics.update(fetch_outcome.diagnostics)
            diagnostics["scrape_layer"] = "playwright"
            parsed = _parse_playwright_fetch(
                scraper,
                fetch_outcome,
                url=url,
                diagnostics=diagnostics,
                captured_at=captured_at,
            )
        else:
            html, pw_diag = await scraper._fetch_html_with_page()
            pw_ms = int((time.perf_counter() - pw_t0) * 1000)
            diagnostics.update(pw_diag)
            diagnostics["scrape_layer"] = "playwright"
            diagnostics["playwright_duration_ms"] = pw_ms
            if http_ms:
                diagnostics["http_duration_ms"] = http_ms
            parsed = scraper._parse_html_to_result(html, extra_raw=diagnostics, captured_at=captured_at)
            parsed = _merge_json_ld_into_result(scraper, parsed, html)

        pw_ms = int((time.perf_counter() - pw_t0) * 1000)
        diagnostics["playwright_duration_ms"] = pw_ms
        if http_ms:
            diagnostics["http_duration_ms"] = http_ms

        if parsed is None or parsed.price is None:
            ms = int((time.perf_counter() - t0) * 1000)
            return _failure_result(
                scraper,
                started,
                ms,
                RuntimeError("price_missing_after_playwright"),
                screenshot_path=None,
                raw_data={
                    **diagnostics,
                    "scrape_error_code": SCRAPE_ERROR_PRICE_NOT_FOUND,
                },
            )
        ms = int((time.perf_counter() - t0) * 1000)
        out = _success_result(
            scraper,
            parsed,
            layer="playwright",
            duration_ms=ms,
            started=started,
            extra=diagnostics,
        )
        _log_scrape_success(
            "playwright",
            url=url,
            duration_ms=ms,
            price=out.price,
            competitor_product_id=competitor_product_id,
        )
        return out
    except Exception as exc:  # noqa: BLE001
        pw_ms = int((time.perf_counter() - pw_t0) * 1000)
        ms = int((time.perf_counter() - t0) * 1000)
        diagnostics["playwright_duration_ms"] = pw_ms
        if http_ms:
            diagnostics["http_duration_ms"] = http_ms
        logger.exception("scraper_failure site=technopolis_bg layer=playwright url=%s", url)
        fail_raw = {**diagnostics, "scrape_layer": "playwright"}
        if not settings.scrape_http_enabled:
            fail_raw["http_skipped"] = True
        return _failure_result(
            scraper,
            started,
            ms,
            exc,
            screenshot_path=None,
            raw_data=fail_raw,
        )
