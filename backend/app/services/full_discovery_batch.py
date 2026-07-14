"""Incremental full-domain product URL discovery (sitemap → batched DB upsert)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Competitor, CompetitorProduct, Product
from app.scrapers.sites.generic_discovery import (
    check_site_reachability,
    collect_generic_product_urls_from_autocomplete,
    collect_generic_product_urls_from_category_pagination,
    collect_generic_product_urls_from_common_crawl,
    collect_generic_product_urls_from_dynamic_endpoints,
    collect_generic_product_urls_from_merchant_feeds,
    collect_generic_product_urls_from_public_pages,
    collect_generic_product_urls_from_search_index,
    collect_generic_product_urls_from_site_search,
    collect_generic_product_urls_from_sitemaps,
    collect_generic_product_urls_from_wayback,
    normalize_generic_product_url,
)
from app.scrapers.sites.generic_discovery import _strip_tracking_query  # noqa: PLC2701
from app.scrapers.sites.site_probe import probe_site
from app.scrapers.sites.technopolis_full_discovery import collect_product_urls_from_sitemaps
from app.scrapers.sites.technopolis_urls import (
    normalize_technopolis_product_url,
    parse_technopolis_product_url,
    prefer_technopolis_product_url,
    technopolis_product_code,
)
from app.services.competitor_category_builder import ensure_category_path_for_competitor_product
from app.services.competitor_category_service import refresh_category_product_counts
from app.utils.url_utils import is_technopolis, normalize_domain

BATCH_SIZE = 1000
MAX_CATALOG_SEARCH_TERMS = 80
LOW_YIELD_URL_THRESHOLD = 25
DISCOVERY_METHOD_SITEMAP = "sitemap"
DISCOVERY_METHOD_CATEGORY_PAGINATION = "category_pagination"
DISCOVERY_METHOD_EXTERNAL_SEARCH = "external_search"
DISCOVERY_METHOD_DYNAMIC_ENDPOINTS = "dynamic_endpoints"
DISCOVERY_METHOD_SITE_SEARCH = "site_search"
DISCOVERY_METHOD_MERCHANT_FEEDS = "merchant_feeds"
DISCOVERY_METHOD_AUTOCOMPLETE = "autocomplete"
DEFAULT_DISCOVERY_METHODS = [DISCOVERY_METHOD_SITEMAP]
ALL_DISCOVERY_METHODS = {
    DISCOVERY_METHOD_SITEMAP,
    DISCOVERY_METHOD_CATEGORY_PAGINATION,
    DISCOVERY_METHOD_EXTERNAL_SEARCH,
    DISCOVERY_METHOD_DYNAMIC_ENDPOINTS,
    DISCOVERY_METHOD_SITE_SEARCH,
    DISCOVERY_METHOD_MERCHANT_FEEDS,
    DISCOVERY_METHOD_AUTOCOMPLETE,
}
# Auto mode tries methods in the probe's rank order (best first) and STOPS at
# the first one that discovers a real batch of products — the best working path.
# The remaining methods are fallbacks, run only if the higher-ranked ones came
# up short (blocked/empty). This avoids running every method one-after-another.
_AUTO_METHOD_TAIL_ORDER = [
    DISCOVERY_METHOD_SITEMAP,
    DISCOVERY_METHOD_MERCHANT_FEEDS,
    DISCOVERY_METHOD_DYNAMIC_ENDPOINTS,
    DISCOVERY_METHOD_CATEGORY_PAGINATION,
    DISCOVERY_METHOD_AUTOCOMPLETE,
    DISCOVERY_METHOD_EXTERNAL_SEARCH,
    DISCOVERY_METHOD_SITE_SEARCH,
]
# In auto mode, a method that adds at least this many new product URLs counts as
# the winning path — later (lower-ranked) methods are then skipped.
_AUTO_EARLY_STOP_MIN_URLS = 5

ProgressCallback = Callable[[dict[str, Any]], None]


class _DiscoveryCancelled(Exception):
    """Raised from the progress callback when a cooperative stop is requested;
    unwinds the running method so URLs found so far still get persisted."""


@dataclass
class DiscoveredListing:
    url: str
    product_code: str | None
    fallback_slug: str | None


@dataclass
class FullDiscoveryStats:
    product_urls_found: int = 0
    new_urls_found: int = 0
    created: int = 0
    skipped_existing: int = 0
    categories_updated: int = 0
    sitemap_files_checked: int = 0
    pages_scanned: int = 0
    external_queries_checked: int = 0
    rate_limit_pauses: int = 0
    duration_ms: int = 0
    sample_new_urls: list[str] = field(default_factory=list)
    sample_existing_urls: list[str] = field(default_factory=list)
    sample_product_urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    public_discovery_blocked: bool = False
    discovery_block_reason: str | None = None
    deep_discovery: bool = False
    seed_terms_used: int = 0
    discovery_methods: list[dict[str, Any]] = field(default_factory=list)
    probe: dict[str, Any] | None = None
    current_phase: str = "reading_sitemap_index"
    current: int = 0
    total: int = 0
    cancelled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_urls_found": self.product_urls_found,
            "new_urls_found": self.new_urls_found,
            "created": self.created,
            "skipped_existing": self.skipped_existing,
            "categories_updated": self.categories_updated,
            "sitemap_files_checked": self.sitemap_files_checked,
            "pages_scanned": self.pages_scanned,
            "external_queries_checked": self.external_queries_checked,
            "rate_limit_pauses": self.rate_limit_pauses,
            "duration_ms": self.duration_ms,
            "sample_new_urls": self.sample_new_urls,
            "sample_existing_urls": self.sample_existing_urls,
            "sample_product_urls": self.sample_product_urls,
            "errors": self.errors,
            "public_discovery_blocked": self.public_discovery_blocked,
            "discovery_block_reason": self.discovery_block_reason,
            "deep_discovery": self.deep_discovery,
            "seed_terms_used": self.seed_terms_used,
            "discovery_methods": self.discovery_methods,
            "probe": self.probe,
            "current_phase": self.current_phase,
            "current": self.current,
            "total": self.total,
            "cancelled": self.cancelled,
        }


def _dedupe_discovered_urls(raw_urls: list[str], *, domain: str = "technopolis.bg") -> list[DiscoveredListing]:
    """Normalize URLs and keep one listing per product URL/code."""
    code_to_listing: dict[str, DiscoveredListing] = {}
    no_code: list[DiscoveredListing] = []
    is_tech = is_technopolis(domain)

    for raw in raw_urls:
        normalized = (
            normalize_technopolis_product_url(raw)
            if is_tech
            else normalize_generic_product_url(raw, domain=domain)
        )
        if not normalized:
            continue
        code = technopolis_product_code(normalized) if is_tech else None
        parsed = parse_technopolis_product_url(normalized) if is_tech else None
        fallback_slug = parsed.get("url_category_slug") if parsed else None
        listing = DiscoveredListing(
            url=normalized,
            product_code=code,
            fallback_slug=str(fallback_slug) if fallback_slug else None,
        )
        if code:
            prev = code_to_listing.get(code)
            if prev is None:
                code_to_listing[code] = listing
            else:
                preferred = prefer_technopolis_product_url(prev.url, normalized)
                if preferred != prev.url:
                    parsed = parse_technopolis_product_url(preferred)
                    code_to_listing[code] = DiscoveredListing(
                        url=preferred,
                        product_code=code,
                        fallback_slug=str(parsed["url_category_slug"]) if parsed else prev.fallback_slug,
                    )
        else:
            no_code.append(listing)

    seen_urls: set[str] = set()
    out: list[DiscoveredListing] = []
    for listing in list(code_to_listing.values()) + no_code:
        if listing.url in seen_urls:
            continue
        seen_urls.add(listing.url)
        out.append(listing)
    return out


def _url_path_base(url: str) -> str:
    """URL without any query string or trailing slash — candidate-fetch key."""
    return url.split("?", 1)[0].rstrip("/")


def _url_dedupe_key(url: str) -> str:
    """Duplicate-check key: strips listing/tracking params (?page, ?sort, utm_…)
    but KEEPS identifying ones (?product_id=…), so query-routed shops where the
    path is shared by every product don't collapse into one listing."""
    path, _, query = url.partition("?")
    kept = _strip_tracking_query(query)
    return path.rstrip("/") + (f"?{kept}" if kept else "")


def _existing_by_urls(
    db: Session,
    competitor_id: uuid.UUID,
    urls: list[str],
) -> dict[str, tuple[uuid.UUID, uuid.UUID | None]]:
    """Return url → (id, competitor_category_id) for this competitor only.

    Matches on the dedupe key (listing/tracking params ignored on BOTH sides),
    so a discovered URL can never duplicate an existing row that differs only
    by pagination/sort/utm noise — and vice versa when old rows are dirty.
    """
    if not urls:
        return {}
    key_of = {u: _url_dedupe_key(u) for u in urls}
    stmt = select(
        CompetitorProduct.url,
        CompetitorProduct.id,
        CompetitorProduct.competitor_category_id,
    ).where(CompetitorProduct.competitor_id == competitor_id)
    if db.get_bind().dialect.name == "postgresql":
        # Fetch by query-stripped path (a superset of the dedupe key match),
        # then refine with the exact key in Python.
        db_path = func.rtrim(func.split_part(CompetitorProduct.url, "?", 1), "/")
        stmt = stmt.where(db_path.in_(list({_url_path_base(u) for u in urls})))
    # else (sqlite in tests): no split_part — filter in Python below.
    by_key: dict[str, tuple[uuid.UUID, uuid.UUID | None]] = {}
    wanted = set(key_of.values())
    for url, row_id, cat_id in db.execute(stmt):
        key = _url_dedupe_key(str(url))
        if key in wanted:
            by_key.setdefault(key, (row_id, cat_id))
    return {u: by_key[key] for u, key in key_of.items() if key in by_key}


def _existing_by_product_codes(
    db: Session,
    competitor_id: uuid.UUID,
    codes: list[str],
) -> dict[str, tuple[uuid.UUID, str, uuid.UUID | None]]:
    """Return product_code → (id, url, competitor_category_id)."""
    if not codes:
        return {}
    rows = db.execute(
        select(
            CompetitorProduct.technopolis_product_code,
            CompetitorProduct.id,
            CompetitorProduct.url,
            CompetitorProduct.competitor_category_id,
        ).where(
            CompetitorProduct.competitor_id == competitor_id,
            CompetitorProduct.technopolis_product_code.in_(codes),
        ),
    ).all()
    out: dict[str, tuple[uuid.UUID, str, uuid.UUID | None]] = {}
    for code, row_id, url, cat_id in rows:
        if code:
            out[str(code)] = (row_id, str(url), cat_id)
    return out


def _report(progress: ProgressCallback | None, stats: FullDiscoveryStats) -> None:
    if progress is not None:
        progress(stats.as_dict())


def _blocked_public_discovery_reason(errors: list[str]) -> str | None:
    if not errors:
        return None
    blocked = [
        error
        for error in errors
        if "status_401:" in error
        or "status_403:" in error
        or "status_429:" in error
        or "blocked_challenge:" in error
    ]
    if not blocked:
        return None
    if any("blocked_challenge:" in error for error in blocked):
        return "challenge_or_captcha"
    if any("status_403:" in error for error in blocked):
        return "http_403_forbidden"
    if any("status_429:" in error for error in blocked):
        return "rate_limited"
    return "access_denied"


def _merge_discovery_diag(base: dict[str, Any], key: str, diag: dict[str, Any]) -> dict[str, Any]:
    return {
        **base,
        "source": f"{base.get('source', 'generic')}+{diag.get('source', key)}",
        key: diag,
        "pages_scanned": max(int(base.get("pages_scanned", 0) or 0), int(diag.get("pages_scanned", 0) or 0)),
        "external_queries_checked": int(base.get("external_queries_checked", 0) or 0)
        + int(diag.get("queries_checked", 0) or 0),
        "rate_limit_pauses": int(base.get("rate_limit_pauses", 0) or 0)
        + int(diag.get("rate_limit_pauses", 0) or 0),
        "sitemap_urls_checked": int(base.get("sitemap_urls_checked", 0) or 0),
        "sample_product_urls": diag.get("sample_product_urls") or base.get("sample_product_urls") or [],
        "errors": [
            *(base.get("errors") or []),
            *(diag.get("errors") or []),
        ],
        "limit_reached": bool(base.get("limit_reached") or diag.get("limit_reached")),
    }


def _normalize_discovery_methods(methods: list[str] | None, *, deep_discovery: bool) -> list[str]:
    if methods:
        out: list[str] = []
        seen: set[str] = set()
        for raw in methods:
            method = str(raw).strip().lower()
            if method not in ALL_DISCOVERY_METHODS or method in seen:
                continue
            seen.add(method)
            out.append(method)
            if len(out) >= len(ALL_DISCOVERY_METHODS):
                break
        return out or list(DEFAULT_DISCOVERY_METHODS)
    if deep_discovery:
        return [
            DISCOVERY_METHOD_SITEMAP,
            DISCOVERY_METHOD_MERCHANT_FEEDS,
            DISCOVERY_METHOD_DYNAMIC_ENDPOINTS,
            DISCOVERY_METHOD_CATEGORY_PAGINATION,
            DISCOVERY_METHOD_AUTOCOMPLETE,
            DISCOVERY_METHOD_EXTERNAL_SEARCH,
            DISCOVERY_METHOD_SITE_SEARCH,
        ]
    return list(DEFAULT_DISCOVERY_METHODS)


def _method_label(method: str) -> str:
    return {
        DISCOVERY_METHOD_SITEMAP: "Sitemap",
        DISCOVERY_METHOD_CATEGORY_PAGINATION: "Category pagination",
        DISCOVERY_METHOD_EXTERNAL_SEARCH: "External search",
        DISCOVERY_METHOD_DYNAMIC_ENDPOINTS: "Dynamic endpoints",
        DISCOVERY_METHOD_SITE_SEARCH: "Site search",
        DISCOVERY_METHOD_MERCHANT_FEEDS: "Merchant feeds",
        DISCOVERY_METHOD_AUTOCOMPLETE: "Autocomplete",
    }.get(method, method)


def _clean_search_term(value: str | None, *, max_len: int = 80) -> str | None:
    if value is None:
        return None
    term = " ".join(value.strip().split())
    if len(term) < 3 or len(term) > max_len:
        return None
    low = term.lower()
    if low in {"none", "null", "n/a", "unknown"}:
        return None
    return term


def _normalize_seed_terms(seed_terms: list[str], limit: int = 100) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in seed_terms:
        for part in raw.replace("\n", ",").split(","):
            term = _clean_search_term(part, max_len=60)
            if term is None:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(term)
            if len(out) >= limit:
                return out
    return out


def _catalog_search_terms(db: Session, limit: int = MAX_CATALOG_SEARCH_TERMS) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if len(terms) >= limit:
            return
        term = _clean_search_term(value)
        if term is None:
            return
        key = term.lower()
        if key in seen:
            return
        seen.add(key)
        terms.append(term)

    rows = db.execute(
        select(
            Product.brand,
            Product.manufacturer_code,
            Product.model,
            Product.ean,
            Product.sku,
            Product.name,
        )
        .where(Product.name.is_not(None))
        .order_by(Product.updated_at.desc())
        .limit(limit * 2),
    ).all()
    for brand, manufacturer_code, model, ean, sku, name in rows:
        add(brand)
        add(model)
        add(manufacturer_code)
        add(ean)
        add(sku)
        if brand and name:
            add(f"{brand} {name}")
        add(name)
        if len(terms) >= limit:
            break
    return terms


def _maybe_update_category(
    db: Session,
    *,
    product_id: uuid.UUID,
    fallback_slug: str | None,
    force_rescan: bool,
) -> bool:
    if not fallback_slug:
        return False
    cp = db.get(CompetitorProduct, product_id)
    if cp is None:
        return False
    if cp.competitor_category_id is not None and not force_rescan:
        return False
    before = cp.competitor_category_id
    ensure_category_path_for_competitor_product(
        db,
        cp,
        breadcrumb_categories=None,
        fallback_category_slug=fallback_slug,
    )
    return cp.competitor_category_id != before


def run_incremental_full_discovery(
    db: Session,
    competitor_id: uuid.UUID,
    *,
    only_new: bool = True,
    force_rescan: bool = False,
    limit: int | None = None,
    source: str = "sitemap",
    discovery_source: str = "full_sitemap",
    deep_discovery: bool = False,
    seed_terms: list[str] | None = None,
    max_search_queries: int | None = None,
    discovery_methods: list[str] | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    Discover product URLs from sitemap, then insert only missing listings in batches.

    Does not scrape prices, match products, or overwrite latest_* fields.
    A cancel request stops the current method (its partial URLs are lost) but
    everything already found is still deduplicated and saved.
    """
    started = time.perf_counter()
    stats = FullDiscoveryStats()
    stats.deep_discovery = deep_discovery
    competitor = db.get(Competitor, competitor_id)
    if competitor is None:
        raise ValueError("competitor_not_found")

    domain = normalize_domain(competitor.domain or "")

    if source not in ("sitemap", "auto"):
        raise ValueError(f"unsupported_discovery_source:{source}")

    def sitemap_progress(payload: dict[str, Any]) -> None:
        if cancel_check is not None and cancel_check():
            raise _DiscoveryCancelled()
        stats.current_phase = payload.get("current_phase", stats.current_phase)
        stats.sitemap_files_checked = int(payload.get("sitemap_files_checked", stats.sitemap_files_checked))
        stats.pages_scanned = int(payload.get("pages_scanned", stats.pages_scanned) or 0)
        stats.external_queries_checked = int(
            payload.get("external_queries_checked", stats.external_queries_checked) or 0,
        )
        stats.rate_limit_pauses = int(payload.get("rate_limit_pauses", stats.rate_limit_pauses) or 0)
        stats.product_urls_found = int(payload.get("product_urls_found", stats.product_urls_found))
        stats.duration_ms = int((time.perf_counter() - started) * 1000)
        _report(progress, stats)

    stats.current_phase = "reading_sitemap_index"
    _report(progress, stats)

    max_products = limit
    normalized_seed_terms = _normalize_seed_terms(seed_terms or [])
    http_blocked = False
    if not is_technopolis(domain):
        # Fail fast when the site drops every connection (IP-level block, dead
        # host) — otherwise each discovery method grinds through 30s timeouts
        # for hours while reporting zero progress.
        stats.current_phase = "checking_site_reachability"
        _report(progress, stats)
        reachability = asyncio.run(check_site_reachability(competitor.domain))
        if not reachability["reachable"]:
            stats.errors.append("site_unreachable:" + ";".join(reachability["errors"][:4]))
            stats.public_discovery_blocked = True
            stats.discovery_block_reason = "site_unreachable"
            stats.current_phase = "completed"
            stats.duration_ms = int((time.perf_counter() - started) * 1000)
            _report(progress, stats)
            result = stats.as_dict()
            result["only_new"] = only_new
            result["force_rescan"] = force_rescan
            result["requested_source"] = source
            result["source"] = "unreachable"
            result["competitor_id"] = str(competitor_id)
            result["deep_discovery"] = deep_discovery
            result["selected_discovery_methods"] = []
            result["limit_reached"] = False
            result["max_products"] = max_products
            return result
        http_blocked = reachability["via"] == "browser"
    auto_mode = source == "auto" and not is_technopolis(domain)
    if auto_mode and http_blocked:
        # Plain HTTP is dropped at the transport level but Chromium gets
        # through. The probe is httpx-only, so running it would just burn
        # minutes of timeouts — go straight to browser-capable methods.
        selected_methods = [
            DISCOVERY_METHOD_CATEGORY_PAGINATION,
            DISCOVERY_METHOD_SITEMAP,
            DISCOVERY_METHOD_DYNAMIC_ENDPOINTS,
            DISCOVERY_METHOD_SITE_SEARCH,
            DISCOVERY_METHOD_EXTERNAL_SEARCH,
        ]
        stats.probe = {
            "platform": None,
            "blocked": False,
            "best_method": DISCOVERY_METHOD_CATEGORY_PAGINATION,
            "recommended_methods": selected_methods,
            "method_reasons": {
                m: "site drops plain HTTP clients; using browser fallback" for m in selected_methods
            },
            "duration_ms": 0,
        }
        _report(progress, stats)
    elif auto_mode:
        # Probe the shop first and let it pick the optimal method order.
        stats.current_phase = "probing_site"
        _report(progress, stats)
        probe_result = asyncio.run(probe_site(competitor.domain))
        stats.probe = {
            "platform": probe_result.get("platform"),
            "blocked": probe_result.get("blocked"),
            "best_method": probe_result.get("best_method"),
            "recommended_methods": probe_result.get("recommended_methods"),
            "method_reasons": probe_result.get("method_reasons"),
            "duration_ms": probe_result.get("duration_ms"),
        }
        selected_methods = list(probe_result.get("recommended_methods") or DEFAULT_DISCOVERY_METHODS)
        # The probe only ranks methods it detected signals for; append the rest
        # so auto still tries every approach and collects the union.
        selected_methods.extend(m for m in _AUTO_METHOD_TAIL_ORDER if m not in selected_methods)
        _report(progress, stats)
    else:
        selected_methods = _normalize_discovery_methods(discovery_methods, deep_discovery=deep_discovery)
    raw_urls: list[str] = []
    flow_seen_urls: set[str] = set()
    sitemap_diag: dict[str, Any] = {
        "source": "selected_methods",
        "sitemap_urls_checked": 0,
        "pages_scanned": 0,
        "external_queries_checked": 0,
        "rate_limit_pauses": 0,
        "errors": [],
        "sample_product_urls": [],
    }

    def add_method_result(method: str, method_urls: list[str], diag: dict[str, Any]) -> None:
        nonlocal sitemap_diag
        added_urls: list[str] = []
        duplicate_count = 0
        for raw_url in method_urls:
            normalized = (
                normalize_technopolis_product_url(raw_url)
                if is_technopolis(domain)
                else normalize_generic_product_url(raw_url, domain=domain)
            )
            if normalized is None:
                continue
            if normalized in flow_seen_urls:
                duplicate_count += 1
                continue
            if max_products is not None and len(flow_seen_urls) >= max_products:
                break
            flow_seen_urls.add(normalized)
            raw_urls.append(raw_url)
            added_urls.append(raw_url)

        errors = [str(e) for e in diag.get("errors") or []]
        block_reason = _blocked_public_discovery_reason(errors)
        result = {
            "method": method,
            "label": _method_label(method),
            "status": "blocked" if block_reason and not method_urls else "completed",
            "found": len(method_urls),
            "added": len(added_urls),
            "skipped_duplicate": duplicate_count,
            "blocked": bool(block_reason and not method_urls),
            "block_reason": block_reason,
            "sample_urls": added_urls[:5] or method_urls[:5],
            "errors": errors[:8],
        }
        stats.discovery_methods.append(result)
        stats.product_urls_found = len(flow_seen_urls)
        stats.duration_ms = int((time.perf_counter() - started) * 1000)
        sitemap_diag = _merge_discovery_diag(sitemap_diag, method, diag)
        _report(progress, stats)
        return len(added_urls)

    if is_technopolis(domain):
        tech_urls, tech_diag = asyncio.run(
            collect_product_urls_from_sitemaps(
                max_products=max_products,
                progress_callback=sitemap_progress,
            ),
        )
        add_method_result(DISCOVERY_METHOD_SITEMAP, tech_urls, {
            **tech_diag,
            "sitemap_urls_checked": tech_diag.get("sitemap_urls_checked", 0),
            "product_urls_found": len(tech_urls),
        })
    elif source in ("sitemap", "auto"):
        try:
            for method in selected_methods:
                if max_products is not None and len(flow_seen_urls) >= max_products:
                    break
                if cancel_check is not None and cancel_check():
                    raise _DiscoveryCancelled()
                if method == DISCOVERY_METHOD_SITEMAP:
                    stats.current_phase = "reading_sitemap_index"
                    _report(progress, stats)
                    method_urls, method_diag = asyncio.run(
                        collect_generic_product_urls_from_sitemaps(
                            competitor.domain,
                            max_products=max_products,
                            progress_callback=sitemap_progress,
                        ),
                    )
                elif method == DISCOVERY_METHOD_CATEGORY_PAGINATION:
                    stats.current_phase = "category_pagination"
                    _report(progress, stats)
                    method_urls, method_diag = asyncio.run(
                        collect_generic_product_urls_from_category_pagination(
                            competitor.domain,
                            max_products=max_products,
                            progress_callback=sitemap_progress,
                        ),
                    )
                elif method == DISCOVERY_METHOD_EXTERNAL_SEARCH:
                    stats.current_phase = "searching_external_indexes"
                    _report(progress, stats)
                    catalog_terms = _catalog_search_terms(db, limit=200 if deep_discovery else MAX_CATALOG_SEARCH_TERMS)
                    extra_terms = [*normalized_seed_terms, *catalog_terms]
                    stats.seed_terms_used = len(extra_terms)
                    search_query_budget = max_search_queries or (160 if deep_discovery else 48)
                    search_urls, search_diag = asyncio.run(
                        collect_generic_product_urls_from_search_index(
                            competitor.domain,
                            max_products=max_products,
                            max_queries=search_query_budget,
                            extra_terms=extra_terms,
                            patient_mode=deep_discovery,
                            progress_callback=sitemap_progress,
                        ),
                    )
                    common_crawl_urls, common_crawl_diag = asyncio.run(
                        collect_generic_product_urls_from_common_crawl(
                            competitor.domain,
                            max_products=max_products,
                            progress_callback=sitemap_progress,
                        ),
                    )
                    wayback_urls, wayback_diag = asyncio.run(
                        collect_generic_product_urls_from_wayback(
                            competitor.domain,
                            max_products=max_products,
                            progress_callback=sitemap_progress,
                        ),
                    )
                    method_urls = [*search_urls, *common_crawl_urls, *wayback_urls]
                    method_diag = _merge_discovery_diag(
                        _merge_discovery_diag(search_diag, "common_crawl", common_crawl_diag),
                        "wayback",
                        wayback_diag,
                    )
                elif method == DISCOVERY_METHOD_DYNAMIC_ENDPOINTS:
                    stats.current_phase = "sniffing_dynamic_endpoints"
                    _report(progress, stats)
                    method_urls, method_diag = asyncio.run(
                        collect_generic_product_urls_from_dynamic_endpoints(
                            competitor.domain,
                            max_products=max_products,
                            progress_callback=sitemap_progress,
                        ),
                    )
                elif method == DISCOVERY_METHOD_MERCHANT_FEEDS:
                    stats.current_phase = "reading_merchant_feeds"
                    _report(progress, stats)
                    method_urls, method_diag = asyncio.run(
                        collect_generic_product_urls_from_merchant_feeds(
                            competitor.domain,
                            max_products=max_products,
                            progress_callback=sitemap_progress,
                        ),
                    )
                elif method == DISCOVERY_METHOD_AUTOCOMPLETE:
                    stats.current_phase = "probing_autocomplete"
                    _report(progress, stats)
                    method_urls, method_diag = asyncio.run(
                        collect_generic_product_urls_from_autocomplete(
                            competitor.domain,
                            max_products=max_products,
                            progress_callback=sitemap_progress,
                        ),
                    )
                elif method == DISCOVERY_METHOD_SITE_SEARCH:
                    stats.current_phase = "site_search"
                    _report(progress, stats)
                    site_terms = [*normalized_seed_terms]
                    stats.seed_terms_used = max(stats.seed_terms_used, len(site_terms))
                    method_urls, method_diag = asyncio.run(
                        collect_generic_product_urls_from_site_search(
                            competitor.domain,
                            search_terms=site_terms,
                            max_products=max_products,
                            progress_callback=sitemap_progress,
                        ),
                    )
                else:
                    continue
                added = add_method_result(method, method_urls, method_diag)
                # Auto mode: the first ranked method to find a real batch of
                # products is the best path — stop instead of running the rest.
                if auto_mode and added >= _AUTO_EARLY_STOP_MIN_URLS:
                    break
        except _DiscoveryCancelled:
            stats.cancelled = True
            stats.errors.append(f"run_stopped:cancel_requested_during:{method}")
            _report(progress, stats)
    else:
        raise ValueError(f"unsupported_discovery_source:{source}")
    stats.sitemap_files_checked = int(
        sitemap_diag.get("sitemap_urls_checked", stats.sitemap_files_checked),
    )
    stats.pages_scanned = int(sitemap_diag.get("pages_scanned", stats.pages_scanned) or 0)
    stats.external_queries_checked = int(
        sitemap_diag.get("external_queries_checked", stats.external_queries_checked) or 0,
    )
    stats.rate_limit_pauses = int(sitemap_diag.get("rate_limit_pauses", stats.rate_limit_pauses) or 0)
    stats.errors.extend(str(e) for e in sitemap_diag.get("errors") or [])
    stats.sample_product_urls = list(sitemap_diag.get("sample_product_urls") or raw_urls[:10])

    stats.current_phase = "deduplicating"
    listings = _dedupe_discovered_urls(raw_urls, domain=domain)
    stats.product_urls_found = len(listings)
    stats.total = len(listings)
    if not listings:
        block_reason = _blocked_public_discovery_reason(stats.errors)
        if block_reason is not None:
            stats.public_discovery_blocked = True
            stats.discovery_block_reason = block_reason
    stats.duration_ms = int((time.perf_counter() - started) * 1000)
    _report(progress, stats)

    now = datetime.now(timezone.utc)
    sample_new: list[str] = []
    sample_existing: list[str] = []

    for batch_start in range(0, len(listings), BATCH_SIZE):
        batch = listings[batch_start : batch_start + BATCH_SIZE]
        stats.current_phase = "checking_existing_urls"
        stats.current = batch_start
        stats.duration_ms = int((time.perf_counter() - started) * 1000)
        _report(progress, stats)

        url_map = _existing_by_urls(db, competitor_id, [x.url for x in batch])
        code_map = _existing_by_product_codes(
            db,
            competitor_id,
            [x.product_code for x in batch if x.product_code],
        )

        to_create: list[DiscoveredListing] = []
        category_updates: list[tuple[uuid.UUID, str | None]] = []

        for listing in batch:
            existing = url_map.get(listing.url)
            if existing is None and listing.product_code:
                by_code = code_map.get(listing.product_code)
                if by_code is not None:
                    existing = (by_code[0], by_code[2])

            if existing is not None:
                stats.skipped_existing += 1
                if len(sample_existing) < 5:
                    sample_existing.append(listing.url)
                if listing.fallback_slug and (force_rescan or existing[1] is None):
                    category_updates.append((existing[0], listing.fallback_slug))
                continue

            to_create.append(listing)

        stats.new_urls_found += len(to_create)
        stats.current_phase = "saving_new_products"
        stats.duration_ms = int((time.perf_counter() - started) * 1000)
        _report(progress, stats)

        for listing in to_create:
            db.add(
                CompetitorProduct(
                    competitor_id=competitor_id,
                    url=listing.url,
                    technopolis_product_code=listing.product_code,
                    discovered_at=now,
                    discovery_source=discovery_source,
                ),
            )
            stats.created += 1
            if len(sample_new) < 5:
                sample_new.append(listing.url)

        db.commit()
        db.expire_all()
        stats.current = min(batch_start + len(batch), stats.total)
        stats.duration_ms = int((time.perf_counter() - started) * 1000)
        _report(progress, stats)

        if category_updates:
            stats.current_phase = "updating_categories"
            _report(progress, stats)
            for product_id, fallback_slug in category_updates:
                if _maybe_update_category(
                    db,
                    product_id=product_id,
                    fallback_slug=fallback_slug,
                    force_rescan=force_rescan,
                ):
                    stats.categories_updated += 1
            db.commit()
            db.expire_all()
            stats.duration_ms = int((time.perf_counter() - started) * 1000)
            _report(progress, stats)

    refresh_category_product_counts(db, competitor_id)
    db.commit()

    stats.sample_new_urls = sample_new
    stats.sample_existing_urls = sample_existing
    stats.current = stats.total
    stats.current_phase = "cancelled" if stats.cancelled else "completed"
    stats.duration_ms = int((time.perf_counter() - started) * 1000)
    _report(progress, stats)

    result = stats.as_dict()
    result["only_new"] = only_new
    result["force_rescan"] = force_rescan
    result["requested_source"] = source
    result["source"] = sitemap_diag.get("source", source)
    result["competitor_id"] = str(competitor_id)
    result["deep_discovery"] = deep_discovery
    result["selected_discovery_methods"] = selected_methods
    result["seed_terms_used"] = stats.seed_terms_used
    result["external_queries_checked"] = stats.external_queries_checked
    result["rate_limit_pauses"] = stats.rate_limit_pauses
    result["limit_reached"] = bool(
        sitemap_diag.get("limit_reached")
        or (max_products is not None and len(raw_urls) >= max_products)
    )
    result["max_products"] = max_products
    return result
