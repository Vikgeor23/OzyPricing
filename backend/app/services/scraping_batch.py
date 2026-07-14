"""Batch-scrape competitor listings (concurrent hybrid HTTP + Playwright pool)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator
from urllib.parse import urlparse

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import Competitor, CompetitorProduct
from app.scrapers.base import ScrapeResult
from app.scrapers.sites.generic import _USER_AGENT as _GENERIC_USER_AGENT
from app.scrapers.sites.generic_playwright_pool import GenericPlaywrightPool
from app.scrapers.sites.technopolis_playwright_pool import TechnopolisPlaywrightPool
from app.services.adaptive_concurrency import AdaptiveConcurrencyController
from app.services.scrape_errors import (
    HARD_FAIL_SKIP_CODES,
    SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT,
    SCRAPE_ERROR_RATE_LIMITED,
    classify_scrape_failure,
)
from app.scrapers.sites.douglas_bulk import detect_magento_bulk_transport, fetch_magento_bulk_entries
from app.services.scrape_fetch import fetch_scrape_result_for_listing, scrape_layer_from_result
from app.services.scrape_persist import apply_scrape_result_to_listing
from app.utils.url_utils import is_technopolis, normalize_domain, normalize_url

logger = logging.getLogger(__name__)

CP_BATCH_SIZE = 100


@dataclass
class ScrapeBatchMetrics:
    occ_api_success: int = 0
    occ_api_failed: int = 0
    adaptive_fast_success: int = 0
    adaptive_playwright_success: int = 0
    lightweight_success: int = 0
    playwright_fallback: int = 0
    js_extract_success: int = 0
    occ_ms_total: int = 0
    occ_ms_count: int = 0
    failed: int = 0
    success_count: int = 0
    attempt_count: int = 0
    retry_count: int = 0
    total_scrape_ms: int = 0
    scrape_count: int = 0
    http_skipped: int = 0
    http_ms_total: int = 0
    http_ms_count: int = 0
    playwright_ms_total: int = 0
    playwright_ms_count: int = 0
    failed_by_reason: dict[str, int] = field(default_factory=dict)

    def record(self, result: ScrapeResult, duration_ms: int) -> None:
        self.scrape_count += 1
        self.total_scrape_ms += duration_ms
        layer = scrape_layer_from_result(result)
        raw = result.raw_data
        self.attempt_count += 1

        if raw.get("playwright_retry"):
            self.retry_count += 1

        if raw.get("http_skipped"):
            self.http_skipped += 1

        http_ms = raw.get("http_duration_ms")
        if isinstance(http_ms, int) and http_ms > 0:
            self.http_ms_total += http_ms
            self.http_ms_count += 1

        pw_ms = raw.get("playwright_duration_ms")
        if isinstance(pw_ms, int) and pw_ms > 0:
            self.playwright_ms_total += pw_ms
            self.playwright_ms_count += 1

        occ_ms = raw.get("occ_api_duration_ms")
        if isinstance(occ_ms, int) and occ_ms > 0:
            self.occ_ms_total += occ_ms
            self.occ_ms_count += 1

        if raw.get("occ_api_failed"):
            self.occ_api_failed += 1

        if result.raw_data.get("scraper_status") == "failure":
            self.failed += 1
            code = raw.get("scrape_error_code") or classify_scrape_failure(raw_data=raw)
            self.failed_by_reason[code] = self.failed_by_reason.get(code, 0) + 1
        else:
            self.success_count += 1
            if layer == "occ_api":
                self.occ_api_success += 1
                self.adaptive_fast_success += 1
            elif raw.get("parse_mode") == "js_evaluate":
                self.js_extract_success += 1
                self.adaptive_playwright_success += 1
            elif layer == "http":
                self.lightweight_success += 1
                self.adaptive_fast_success += 1
            elif layer == "playwright":
                self.playwright_fallback += 1
                self.adaptive_playwright_success += 1

    def success_rate_pct(self) -> float:
        if not self.attempt_count:
            return 0.0
        return round(100.0 * self.success_count / self.attempt_count, 1)

    def as_dict(
        self,
        *,
        wall_duration_ms: int,
        current_concurrency: int,
        timeout_pct: float,
        dead_urls_skipped: int,
    ) -> dict:
        avg_ms = int(self.total_scrape_ms / self.scrape_count) if self.scrape_count else 0
        if wall_duration_ms > 0 and self.scrape_count > 0:
            products_per_minute = round(self.scrape_count / (wall_duration_ms / 60_000), 2)
        else:
            products_per_minute = 0.0
        avg_http_ms = int(self.http_ms_total / self.http_ms_count) if self.http_ms_count else 0
        avg_playwright_ms = (
            int(self.playwright_ms_total / self.playwright_ms_count) if self.playwright_ms_count else 0
        )
        avg_occ_ms = int(self.occ_ms_total / self.occ_ms_count) if self.occ_ms_count else 0
        return {
            "occ_api_success": self.occ_api_success,
            "occ_api_failed": self.occ_api_failed,
            "avg_occ_ms": avg_occ_ms,
            "adaptive_fast_success": self.adaptive_fast_success,
            "adaptive_playwright_success": self.adaptive_playwright_success,
            "lightweight_success": self.lightweight_success,
            "playwright_fallback": self.playwright_fallback,
            "js_extract_success": self.js_extract_success,
            "avg_scrape_ms": avg_ms,
            "products_per_minute": products_per_minute,
            "http_skipped": self.http_skipped,
            "avg_http_ms": avg_http_ms,
            "avg_playwright_ms": avg_playwright_ms,
            "failed_by_reason": dict(self.failed_by_reason),
            "current_concurrency": current_concurrency,
            "retry_count": self.retry_count,
            "timeout_pct": timeout_pct,
            "success_pct": self.success_rate_pct(),
            "dead_urls_skipped": dead_urls_skipped,
        }


def _scrape_ids_stmt(
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None,
    only_missing: bool,
    only_stale: bool,
    stale_hours: int,
    skip_recent_failures: bool,
    recent_failure_hours: int,
    skip_dead_urls: bool,
):
    stmt = select(CompetitorProduct.id).where(CompetitorProduct.competitor_id == competitor_id)
    if skip_dead_urls:
        stmt = stmt.where(CompetitorProduct.is_dead.is_(False))
    if category_id is not None:
        stmt = stmt.where(CompetitorProduct.competitor_category_id == category_id)

    if only_missing:
        stmt = stmt.where(
            or_(
                CompetitorProduct.latest_scraped_at.is_(None),
                CompetitorProduct.latest_price.is_(None),
            ),
        )

    if only_stale:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
        stmt = stmt.where(
            or_(
                CompetitorProduct.latest_scraped_at.is_(None),
                CompetitorProduct.latest_scraped_at < cutoff,
            ),
        )

    if skip_recent_failures:
        fail_cutoff = datetime.now(timezone.utc) - timedelta(hours=recent_failure_hours)
        stmt = stmt.where(
            not_(
                and_(
                    CompetitorProduct.latest_scrape_status == "failed",
                    CompetitorProduct.latest_scrape_error_code.in_(HARD_FAIL_SKIP_CODES),
                    CompetitorProduct.latest_scraped_at.isnot(None),
                    CompetitorProduct.latest_scraped_at >= fail_cutoff,
                ),
            ),
        )

    return stmt.order_by(CompetitorProduct.created_at.desc())


def _count_dead_urls_in_scope(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None,
    only_missing: bool,
    only_stale: bool,
    stale_hours: int,
    skip_recent_failures: bool,
    recent_failure_hours: int,
) -> int:
    """Dead listings in the same filter scope (excluded when skip_dead_urls is on)."""
    stmt = select(func.count()).select_from(CompetitorProduct).where(
        CompetitorProduct.competitor_id == competitor_id,
        CompetitorProduct.is_dead.is_(True),
    )
    if category_id is not None:
        stmt = stmt.where(CompetitorProduct.competitor_category_id == category_id)
    if only_missing:
        stmt = stmt.where(
            or_(
                CompetitorProduct.latest_scraped_at.is_(None),
                CompetitorProduct.latest_price.is_(None),
            ),
        )
    if only_stale:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
        stmt = stmt.where(
            or_(
                CompetitorProduct.latest_scraped_at.is_(None),
                CompetitorProduct.latest_scraped_at < cutoff,
            ),
        )
    if skip_recent_failures:
        fail_cutoff = datetime.now(timezone.utc) - timedelta(hours=recent_failure_hours)
        stmt = stmt.where(
            not_(
                and_(
                    CompetitorProduct.latest_scrape_status == "failed",
                    CompetitorProduct.latest_scrape_error_code.in_(HARD_FAIL_SKIP_CODES),
                    CompetitorProduct.latest_scraped_at.isnot(None),
                    CompetitorProduct.latest_scraped_at >= fail_cutoff,
                ),
            ),
        )
    return int(db.scalar(stmt) or 0)


def _count_scrape_targets(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None,
    only_missing: bool,
    only_stale: bool,
    stale_hours: int,
    skip_recent_failures: bool,
    recent_failure_hours: int,
    skip_dead_urls: bool,
) -> int:
    stmt = _scrape_ids_stmt(
        competitor_id=competitor_id,
        category_id=category_id,
        only_missing=only_missing,
        only_stale=only_stale,
        stale_hours=stale_hours,
        skip_recent_failures=skip_recent_failures,
        recent_failure_hours=recent_failure_hours,
        skip_dead_urls=skip_dead_urls,
    )
    return int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


def _iter_scrape_target_ids(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None,
    only_missing: bool,
    only_stale: bool,
    stale_hours: int,
    limit: int | None,
    skip_recent_failures: bool,
    recent_failure_hours: int,
    skip_dead_urls: bool,
) -> Iterator[list[uuid.UUID]]:
    processed = 0
    last_id: uuid.UUID | None = None
    base = _scrape_ids_stmt(
        competitor_id=competitor_id,
        category_id=category_id,
        only_missing=only_missing,
        only_stale=only_stale,
        stale_hours=stale_hours,
        skip_recent_failures=skip_recent_failures,
        recent_failure_hours=recent_failure_hours,
        skip_dead_urls=skip_dead_urls,
    )
    # Keyset pagination by id: scraped rows drop out of the only_missing /
    # only_stale filter mid-run, so OFFSET would skip an unprocessed row for
    # every processed one (half the catalog per pass).
    base = base.order_by(None).order_by(CompetitorProduct.id)

    while True:
        batch_limit = CP_BATCH_SIZE
        if limit is not None:
            remaining = limit - processed
            if remaining <= 0:
                break
            batch_limit = min(batch_limit, remaining)

        stmt = base if last_id is None else base.where(CompetitorProduct.id > last_id)
        ids = list(db.scalars(stmt.limit(batch_limit)).all())
        if not ids:
            break
        yield ids
        processed += len(ids)
        last_id = ids[-1]
        if limit is not None and processed >= limit:
            break


async def _scrape_one_listing(
    cp: CompetitorProduct,
    *,
    concurrency: AdaptiveConcurrencyController,
    pool: TechnopolisPlaywrightPool | None,
    generic_pool: GenericPlaywrightPool | None = None,
) -> tuple[uuid.UUID, ScrapeResult | None, int, BaseException | None]:
    async with concurrency.acquire():
        t0 = time.perf_counter()
        timed_out = False
        rate_limited = False
        try:
            domain = cp.competitor.domain if cp.competitor else ""
            use_pool = pool if is_technopolis(domain) or is_technopolis(cp.url) else None
            result = await fetch_scrape_result_for_listing(
                cp.url,
                domain,
                pool=use_pool,
                generic_pool=generic_pool,
                competitor_product_id=str(cp.id),
            )
            duration_ms = int((time.perf_counter() - t0) * 1000)
            if result.raw_data.get("scraper_status") == "failure":
                code = result.raw_data.get("scrape_error_code") or classify_scrape_failure(
                    raw_data=result.raw_data,
                )
                timed_out = code == SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT
                rate_limited = code == SCRAPE_ERROR_RATE_LIMITED
            return cp.id, result, duration_ms, None
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.perf_counter() - t0) * 1000)
            timed_out = classify_scrape_failure(exc=exc) == SCRAPE_ERROR_PLAYWRIGHT_TIMEOUT
            return cp.id, None, duration_ms, exc
        finally:
            if rate_limited:
                await asyncio.sleep(20)
            await concurrency.record_outcome_async(timed_out=timed_out, rate_limited=rate_limited)


async def _run_batch_scrape_async(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None,
    only_missing: bool,
    only_stale: bool,
    stale_hours: int,
    limit: int | None,
    skip_recent_failures: bool,
    recent_failure_hours: int,
    progress_callback: Callable[[dict], None] | None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    settings = get_settings()
    skip_dead_urls = settings.scrape_skip_dead_urls
    dead_urls_skipped = (
        _count_dead_urls_in_scope(
            db,
            competitor_id=competitor_id,
            category_id=category_id,
            only_missing=only_missing,
            only_stale=only_stale,
            stale_hours=stale_hours,
            skip_recent_failures=skip_recent_failures,
            recent_failure_hours=recent_failure_hours,
        )
        if skip_dead_urls
        else 0
    )
    total = _count_scrape_targets(
        db,
        competitor_id=competitor_id,
        category_id=category_id,
        only_missing=only_missing,
        only_stale=only_stale,
        stale_hours=stale_hours,
        skip_recent_failures=skip_recent_failures,
        recent_failure_hours=recent_failure_hours,
        skip_dead_urls=skip_dead_urls,
    )
    if limit is not None:
        total = min(total, limit)

    scraped = 0
    failed = 0
    skipped = 0
    errors: list[str] = []
    current = 0
    metrics = ScrapeBatchMetrics()
    wall_t0 = time.perf_counter()
    last_progress_at = time.monotonic()
    commit_size = max(1, settings.scrape_batch_commit_size)
    pending_since_commit = 0
    competitor = db.get(Competitor, competitor_id)
    # Per-site cap from the competitor settings; None keeps the global limits.
    site_max = competitor.scrape_concurrency_max if competitor is not None else None
    use_playwright_pool = competitor is not None and is_technopolis(competitor.domain)
    def _controller(initial: int, min_limit: int, max_limit: int) -> AdaptiveConcurrencyController:
        effective_max = site_max or max_limit
        return AdaptiveConcurrencyController(
            initial=min(initial, effective_max),
            min_limit=min(min_limit, effective_max),
            max_limit=effective_max,
        )

    if use_playwright_pool:
        concurrency = _controller(
            settings.scrape_concurrency,
            settings.scrape_concurrency_min,
            settings.scrape_concurrency_max,
        )
    elif competitor is not None and "douglas" in competitor.domain.lower():
        concurrency = _controller(
            settings.scrape_douglas_concurrency,
            settings.scrape_douglas_concurrency_min,
            settings.scrape_douglas_concurrency_max,
        )
    else:
        concurrency = _controller(
            settings.scrape_generic_concurrency,
            settings.scrape_generic_concurrency_min,
            settings.scrape_generic_concurrency_max,
        )

    def _live_metrics() -> dict:
        wall_ms = int((time.perf_counter() - wall_t0) * 1000)
        live = metrics.as_dict(
            wall_duration_ms=wall_ms,
            current_concurrency=concurrency.current_limit,
            timeout_pct=concurrency.timeout_rate_pct(),
            dead_urls_skipped=dead_urls_skipped,
        )
        live["concurrency_throughput_ppm"] = concurrency.last_throughput_ppm
        return live

    def _report(phase: str, *, force: bool = False) -> None:
        nonlocal last_progress_at
        if not progress_callback:
            return
        now = time.monotonic()
        if not force and (now - last_progress_at) < settings.scrape_progress_interval_sec:
            return
        last_progress_at = now
        progress_callback(
            {
                "current": current,
                "total": total,
                "scraped": scraped,
                "failed": failed,
                "skipped": skipped,
                "current_phase": phase,
                "competitor_id": str(competitor_id),
                "category_id": str(category_id) if category_id else None,
                "errors": errors[-20:],
                **_live_metrics(),
            },
        )

    _report("scraping", force=True)

    cancelled = False
    blocked_stop = False

    async def _run_batches(
        pool: TechnopolisPlaywrightPool | None,
        generic_pool: GenericPlaywrightPool | None = None,
    ) -> None:
        nonlocal scraped, failed, skipped, current, pending_since_commit, cancelled, blocked_stop
        blocked_streak = 0
        block_streak_limit = max(1, settings.scrape_block_stop_streak)
        for id_batch in _iter_scrape_target_ids(
            db,
            competitor_id=competitor_id,
            category_id=category_id,
            only_missing=only_missing,
            only_stale=only_stale,
            stale_hours=stale_hours,
            limit=limit,
            skip_recent_failures=skip_recent_failures,
            recent_failure_hours=recent_failure_hours,
            skip_dead_urls=skip_dead_urls,
        ):
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            cps = list(
                db.scalars(
                    select(CompetitorProduct)
                    .where(CompetitorProduct.id.in_(id_batch))
                    .options(joinedload(CompetitorProduct.competitor)),
                ).all(),
            )
            by_id = {cp.id: cp for cp in cps}

            tasks = [
                _scrape_one_listing(
                    by_id[cp_id],
                    concurrency=concurrency,
                    pool=pool,
                    generic_pool=generic_pool,
                )
                for cp_id in id_batch
                if cp_id in by_id
            ]
            results = await asyncio.gather(*tasks)
            result_by_id = {row[0]: row for row in results}

            for cp_id in id_batch:
                current += 1
                if cp_id not in by_id:
                    skipped += 1
                    continue

                cp = by_id[cp_id]
                row = result_by_id.get(cp_id)
                if row is None:
                    skipped += 1
                    continue

                _, result, duration_ms, row_exc = row
                if row_exc is not None or result is None:
                    failed += 1
                    code = classify_scrape_failure(exc=row_exc, error_message=str(row_exc) if row_exc else None)
                    metrics.failed_by_reason[code] = metrics.failed_by_reason.get(code, 0) + 1
                    errors.append(f"{cp_id}: {row_exc}")
                    logger.exception("batch_scrape_row_failure competitor_product_id=%s", cp_id)
                    pending_since_commit += 1
                    _report("scraping")
                    continue

                metrics.record(result, duration_ms)
                try:
                    outcome = apply_scrape_result_to_listing(
                        db,
                        cp,
                        result,
                        listing_url=cp.url,
                        task_duration_ms=duration_ms,
                        competitor_product_id=str(cp.id),
                    )
                    if outcome == "scraped":
                        scraped += 1
                        blocked_streak = 0
                    elif outcome == "failed":
                        failed += 1
                        raw = result.raw_data or {}
                        if raw.get("blocked_signal") or raw.get("scrape_error_code") in (
                            "http_blocked",
                            "rate_limited",
                        ):
                            blocked_streak += 1
                        else:
                            blocked_streak = 0
                    else:
                        skipped += 1
                    pending_since_commit += 1
                    if pending_since_commit >= commit_size:
                        db.commit()
                        pending_since_commit = 0
                        _report("scraping", force=True)
                    else:
                        _report("scraping")
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    failed += 1
                    code = classify_scrape_failure(exc=exc, error_message=str(exc))
                    metrics.failed_by_reason[code] = metrics.failed_by_reason.get(code, 0) + 1
                    errors.append(f"{cp_id}: {exc}")
                    logger.exception("batch_scrape_persist_failure competitor_product_id=%s", cp_id)

            if pending_since_commit > 0:
                db.commit()
                pending_since_commit = 0
                _report("scraping", force=True)

            if blocked_streak >= block_streak_limit:
                blocked_stop = True
                errors.append(
                    f"site_blocking_detected: stopped after {blocked_streak} consecutive "
                    "blocked responses (captcha / anti-bot); wait for the block to lift "
                    "before rescraping",
                )
                logger.warning(
                    "batch_scrape_blocked_stop competitor_id=%s streak=%s current=%s",
                    competitor_id,
                    blocked_streak,
                    current,
                )
                break

    is_douglas = competitor is not None and "douglas" in competitor.domain.lower()
    if use_playwright_pool:
        async with TechnopolisPlaywrightPool() as pool:
            await _run_batches(pool)
    elif is_douglas:
        # Douglas uses its own bulk GraphQL path, not per-page Playwright.
        await _run_batches(None)
    else:
        pool_size = max(1, settings.scrape_generic_browser_pool_size)
        async with GenericPlaywrightPool(
            size=pool_size,
            user_agent=_GENERIC_USER_AGENT,
        ) as generic_pool:
            await _run_batches(None, generic_pool)

    wall_ms = int((time.perf_counter() - wall_t0) * 1000)
    _report("blocked" if blocked_stop else "cancelled" if cancelled else "done", force=True)

    return {
        "competitor_id": str(competitor_id),
        "category_id": str(category_id) if category_id else None,
        "total": total,
        "current": current,
        "scraped": scraped,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
        "duration_ms": wall_ms,
        "cancelled": cancelled,
        "blocked": blocked_stop,
        "skip_recent_failures": skip_recent_failures,
        "recent_failure_hours": recent_failure_hours,
        "skip_dead_urls": skip_dead_urls,
        **_live_metrics(),
    }


def _douglas_match_key(url: str) -> str:
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    host = parsed.netloc.lower().removeprefix("www.")
    return f"{host}{parsed.path.rstrip('/')}"


def _is_douglas_competitor(competitor: Competitor | None) -> bool:
    return competitor is not None and "douglas" in (competitor.domain or "").lower()


def _magento_bulk_transport(competitor: Competitor | None) -> tuple[str, str, bool] | None:
    """Resolve (origin, transport, douglas_rules) when the competitor supports bulk GraphQL.

    Known shops are routed directly; anything else gets a quick probe of its
    /graphql endpoint so new Magento competitors use the fast path without
    configuration. Technopolis keeps its dedicated adapter.
    """
    if competitor is None:
        return None
    settings = get_settings()
    domain = normalize_domain(competitor.domain or "").removeprefix("www.")
    if not domain or is_technopolis(domain):
        return None
    origin = f"https://{domain}"
    if "douglas" in domain:
        if not settings.scrape_douglas_bulk_enabled:
            return None
        return origin, "browser", True
    http_domains = {
        d.strip().removeprefix("www.")
        for d in (settings.scrape_magento_bulk_domains or "").split(",")
        if d.strip()
    }
    if domain in http_domains:
        return origin, "http", False
    if settings.scrape_magento_bulk_autodetect:
        transport = asyncio.run(detect_magento_bulk_transport(origin))
        if transport:
            logger.info("magento_bulk_autodetected domain=%s transport=%s", domain, transport)
            _remember_magento_bulk_domain(domain, transport)
            return origin, transport, False
    return None


def _remember_magento_bulk_domain(domain: str, transport: str) -> None:
    """Persist the autodetect outcome so the stats endpoint can report the
    real scrape method without re-probing the shop."""
    try:
        import redis

        client = redis.Redis.from_url(get_settings().redis_url, socket_timeout=2)
        client.set(f"pm:scrape:magento_bulk:{domain}", transport, ex=60 * 60 * 24 * 30)
    except Exception:  # noqa: BLE001
        logger.warning("magento_bulk_marker_write_failed domain=%s", domain)


def _run_douglas_bulk_scrape(
    db: Session,
    *,
    competitor: Competitor,
    origin: str,
    transport: str,
    douglas_rules: bool,
    category_id: uuid.UUID | None,
    only_missing: bool,
    only_stale: bool,
    stale_hours: int,
    limit: int | None,
    progress_callback: Callable[[dict], None] | None,
) -> dict:
    """Refresh all listings of a Magento shop from its bulk GraphQL feed in one pass.

    Each configurable product contributes one row per variant (own SKU/size);
    variant URLs not present yet are inserted as new listings.
    """
    settings = get_settings()
    wall_t0 = time.perf_counter()
    captured_at = datetime.now(timezone.utc)
    stale_cutoff = captured_at - timedelta(hours=stale_hours)

    def _report(update: dict) -> None:
        if progress_callback:
            progress_callback({"competitor_id": str(competitor.id), **update})

    entries, diag = asyncio.run(
        fetch_magento_bulk_entries(
            origin,
            transport=transport,
            douglas_rules=douglas_rules,
            progress_callback=_report,
        ),
    )
    errors: list[str] = [str(e) for e in diag.get("errors") or []]
    fetch_ms = int(diag.get("duration_ms") or 0)
    per_entry_ms = max(1, fetch_ms // max(1, len(entries)))

    rows = db.scalars(
        select(CompetitorProduct).where(CompetitorProduct.competitor_id == competitor.id),
    ).all()
    by_key: dict[str, CompetitorProduct] = {}
    for cp in rows:
        by_key.setdefault(_douglas_match_key(cp.url), cp)

    if limit is not None:
        entries = entries[:limit]
    total = len(entries)
    scraped = 0
    failed = 0
    skipped = 0
    inserted = 0
    current = 0
    commit_size = max(1, settings.scrape_batch_commit_size)
    pending_since_commit = 0

    def _skip_by_filters(cp: CompetitorProduct) -> bool:
        if category_id is not None and cp.competitor_category_id != category_id:
            return True
        if only_missing and cp.latest_scraped_at is not None:
            return True
        if only_stale and cp.latest_scraped_at is not None and cp.latest_scraped_at > stale_cutoff:
            return True
        return False

    for entry in entries:
        current += 1
        cp = by_key.get(_douglas_match_key(entry.url))
        try:
            if cp is None:
                # Insert unseen URLs only for Douglas, where variant pages are
                # confirmed to resolve. Other Magento shops (e.g. hippoland)
                # return 404 for variant url_keys — there the bulk feed only
                # refreshes rows already discovered via sitemap.
                if category_id is not None or not douglas_rules:
                    skipped += 1
                    continue
                cp = CompetitorProduct(
                    competitor_id=competitor.id,
                    url=entry.url,
                    discovered_at=captured_at,
                    discovery_source="magento_graphql_bulk",
                )
                db.add(cp)
                db.flush()
                by_key[_douglas_match_key(entry.url)] = cp
                inserted += 1
            elif _skip_by_filters(cp):
                skipped += 1
                continue

            result = entry.to_scrape_result(captured_at=captured_at)
            outcome = apply_scrape_result_to_listing(
                db,
                cp,
                result,
                listing_url=cp.url,
                task_duration_ms=per_entry_ms,
                competitor_product_id=str(cp.id),
            )
            if outcome == "scraped":
                scraped += 1
            else:
                failed += 1
            pending_since_commit += 1
            if pending_since_commit >= commit_size:
                db.commit()
                pending_since_commit = 0
                elapsed_min = max((time.perf_counter() - wall_t0) / 60.0, 1e-6)
                _report(
                    {
                        "current": current,
                        "total": total,
                        "scraped": scraped,
                        "failed": failed,
                        "skipped": skipped,
                        "current_phase": "douglas_bulk_applying",
                        "products_per_minute": round(current / elapsed_min, 1),
                        "success_pct": round(100.0 * scraped / max(current, 1), 1),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            pending_since_commit = 0
            failed += 1
            errors.append(f"{entry.url}: {exc}")
            logger.exception("douglas_bulk_persist_failure url=%s", entry.url)

    if pending_since_commit > 0:
        db.commit()

    wall_ms = int((time.perf_counter() - wall_t0) * 1000)
    result_payload = {
        "competitor_id": str(competitor.id),
        "category_id": str(category_id) if category_id else None,
        "total": total,
        "current": current,
        "scraped": scraped,
        "failed": failed,
        "skipped": skipped,
        "errors": errors[:50],
        "duration_ms": wall_ms,
        "source": "magento_graphql_bulk",
        "inserted": inserted,
        "graphql_pages_fetched": diag.get("pages_fetched", 0),
        "products_per_minute": round(scraped / max(wall_ms / 60_000, 0.001), 1),
        "success_pct": round(100.0 * scraped / total, 1) if total else 0.0,
    }
    _report({**result_payload, "current_phase": "done"})
    return result_payload


def run_batch_scrape_competitor_products(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None = None,
    only_missing: bool = False,
    only_stale: bool = False,
    stale_hours: int = 24,
    limit: int | None = None,
    skip_recent_failures: bool | None = None,
    recent_failure_hours: int | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """Concurrent hybrid scrape with batched DB commits and live progress."""
    settings = get_settings()
    if skip_recent_failures is None:
        skip_recent_failures = settings.scrape_skip_recent_failures
    if recent_failure_hours is None:
        recent_failure_hours = settings.scrape_recent_failure_hours

    competitor = db.get(Competitor, competitor_id)
    bulk = _magento_bulk_transport(competitor)
    if bulk is not None:
        origin, transport, douglas_rules = bulk
        bulk_result = _run_douglas_bulk_scrape(
            db,
            competitor=competitor,
            origin=origin,
            transport=transport,
            douglas_rules=douglas_rules,
            category_id=category_id,
            only_missing=only_missing,
            only_stale=only_stale,
            stale_hours=stale_hours,
            limit=limit,
            progress_callback=progress_callback,
        )
        if category_id is not None or limit is not None:
            return bulk_result
        if cancel_check is not None and cancel_check():
            bulk_result["cancelled"] = True
            return bulk_result

        # The catalog feed covers only products the shop exposes there (e.g.
        # hippoland lists 22k of 55k live pages). Everything still without a
        # price gets a per-URL pass in the same run.
        followup = asyncio.run(
            _run_batch_scrape_async(
                db,
                competitor_id=competitor_id,
                category_id=None,
                only_missing=True,
                only_stale=False,
                stale_hours=stale_hours,
                limit=None,
                skip_recent_failures=skip_recent_failures,
                recent_failure_hours=recent_failure_hours,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            ),
        )
        for key in ("total", "current", "scraped", "failed", "skipped"):
            bulk_result[key] = int(bulk_result.get(key, 0) or 0) + int(followup.get(key, 0) or 0)
        bulk_result["duration_ms"] = int(bulk_result.get("duration_ms", 0) or 0) + int(
            followup.get("duration_ms", 0) or 0,
        )
        bulk_result["errors"] = (list(bulk_result.get("errors") or []) + list(followup.get("errors") or []))[:50]
        bulk_result["cancelled"] = bool(followup.get("cancelled"))
        bulk_result["source"] = "magento_graphql_bulk+per_url"
        if bulk_result["duration_ms"] > 0:
            bulk_result["products_per_minute"] = round(
                bulk_result["scraped"] / max(bulk_result["duration_ms"] / 60_000, 0.001),
                1,
            )
        return bulk_result

    return asyncio.run(
        _run_batch_scrape_async(
            db,
            competitor_id=competitor_id,
            category_id=category_id,
            only_missing=only_missing,
            only_stale=only_stale,
            stale_hours=stale_hours,
            limit=limit,
            skip_recent_failures=skip_recent_failures,
            recent_failure_hours=recent_failure_hours,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        ),
    )
