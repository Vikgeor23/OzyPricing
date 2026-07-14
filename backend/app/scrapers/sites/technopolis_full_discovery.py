"""Technopolis.bg — full-domain product URL discovery (sitemap + category crawl)."""

from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import async_playwright

from app.scrapers.sites.technopolis_categories import (
    discover_technopolis_category_nodes,
    is_category_candidate_url,
    is_excluded_url,
)
from app.scrapers.sites.technopolis_discovery import (
    extract_product_urls_from_html,
    find_next_page_url,
)
from app.scrapers.sites.technopolis_urls import (
    is_technopolis_product_url,
    normalize_technopolis_product_url,
    prefer_technopolis_product_url,
    technopolis_product_code,
)
from app.utils.url_utils import TECHNOPOLIS_DEFAULT_START_URL, is_technopolis, normalize_domain

logger = logging.getLogger(__name__)

DEFAULT_SITEMAP_URLS = (
    "https://www.technopolis.bg/sitemap.xml",
    "https://www.technopolis.bg/sitemap_index.xml",
    "https://www.technopolis.bg/bg/sitemap.xml",
)

DEFAULT_MAX_PAGES = 500
DEFAULT_MAX_PRODUCTS: int | None = None

_HTTP_TIMEOUT = 45.0
_MAX_SITEMAP_DEPTH = 8


def resolve_sitemap_loc(base_sitemap_url: str, loc: str) -> str:
    """Public wrapper for tests — resolve relative sitemap ``loc`` values."""
    return _resolve_sitemap_loc(base_sitemap_url, loc)


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _same_domain(url: str, domain: str = "technopolis.bg") -> bool:
    return normalize_domain(url) == normalize_domain(domain)


def _resolve_sitemap_loc(base_sitemap_url: str, loc: str) -> str:
    """Resolve relative sitemap ``loc`` values (e.g. ``/sitemapurl/Product-bg-EUR-0.xml``)."""
    raw = loc.strip()
    if raw.startswith(("http://", "https://")):
        return raw.split("#")[0].strip()
    origin = f"{urlparse(base_sitemap_url).scheme}://{urlparse(base_sitemap_url).netloc}"
    return urljoin(origin + "/", raw.lstrip("/")).split("#")[0].strip()


def _looks_like_sitemap_url(url: str) -> bool:
    low = url.lower()
    if low.endswith(".xml"):
        return True
    if "/sitemapurl/" in low or "sitemap" in low:
        return True
    if "product-bg" in low or "product-en" in low:
        return True
    return False


def _is_product_sitemap_url(url: str) -> bool:
    low = url.lower()
    return "product-bg" in low or "product-en" in low


def _build_parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    parent_map: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent
    return parent_map


def parse_sitemap_locs(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    """
    Parse sitemap XML (namespace-aware via local tag names).

    Returns ``(page_locs, nested_sitemap_locs)``.
    """
    page_locs: list[str] = []
    nested: list[str] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return page_locs, nested

    root_name = _local_tag(root.tag)
    parent_map = _build_parent_map(root)
    seen_page: set[str] = set()
    seen_nested: set[str] = set()

    for elem in root.iter():
        if _local_tag(elem.tag) != "loc" or not elem.text:
            continue
        loc = elem.text.strip()
        parent = parent_map.get(elem)
        parent_name = _local_tag(parent.tag) if parent is not None else ""

        if root_name == "sitemapindex":
            if parent_name == "sitemap" or _looks_like_sitemap_url(loc):
                if loc not in seen_nested:
                    nested.append(loc)
                    seen_nested.add(loc)
            continue

        if root_name == "urlset":
            if parent_name == "url" or parent_name == "urlset" or parent is root:
                if loc not in seen_page:
                    page_locs.append(loc)
                    seen_page.add(loc)
            continue

        # Unknown root: classify by URL shape
        if _looks_like_sitemap_url(loc):
            if loc not in seen_nested:
                nested.append(loc)
                seen_nested.add(loc)
        elif loc not in seen_page:
            page_locs.append(loc)
            seen_page.add(loc)

    return page_locs, nested


def _is_listing_page_url(url: str) -> bool:
    if not is_technopolis(url) or is_excluded_url(url) or is_technopolis_product_url(url):
        return False
    return is_category_candidate_url(url)


class _ProductUrlRegistry:
    """Track product URLs with bg-locale preference per product code."""

    def __init__(self, max_products: int | None) -> None:
        self.max_products = max_products
        self.products: set[str] = set()
        self.code_to_url: dict[str, str] = {}

    def register(self, raw: str) -> bool:
        norm = normalize_technopolis_product_url(raw)
        if not norm:
            return False
        code = technopolis_product_code(norm)
        if not code:
            return False

        existing = self.code_to_url.get(code)
        if existing is not None:
            preferred = prefer_technopolis_product_url(existing, norm)
            if preferred != existing:
                self.products.discard(existing)
                self.products.add(preferred)
                self.code_to_url[code] = preferred
            return False

        if self.max_products is not None and len(self.products) >= self.max_products:
            return False

        self.code_to_url[code] = norm
        self.products.add(norm)
        return True

    def ingest_many(self, urls: list[str]) -> int:
        created = 0
        for raw in urls:
            if self.register(raw):
                created += 1
        return created


def _product_candidates_from_locs(locs: list[str]) -> list[str]:
    out: list[str] = []
    for loc in locs:
        if is_technopolis_product_url(loc):
            norm = normalize_technopolis_product_url(loc)
            if norm:
                out.append(norm)
    return out


def _log_sitemap_file_diag(file_diag: dict[str, Any]) -> None:
    logger.info(
        "technopolis_sitemap parsed sitemap_url=%s loc_count=%s product_candidate_count=%s",
        file_diag.get("sitemap_url"),
        file_diag.get("loc_count"),
        file_diag.get("product_candidate_count"),
    )


async def _fetch_sitemap(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code != 200:
            return None
        content_type = (resp.headers.get("content-type") or "").lower()
        body = resp.content
        if not body.strip():
            return None
        if "xml" not in content_type and not body.strip().startswith(b"<"):
            return None
        return body
    except Exception as exc:  # noqa: BLE001
        logger.debug("sitemap_fetch_failed url=%s err=%s", url, exc)
        return None


async def collect_product_urls_from_sitemaps(
    *,
    sitemap_urls: tuple[str, ...] = DEFAULT_SITEMAP_URLS,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Try Technopolis sitemaps (index + nested product XML) and collect product PDP URLs."""
    t0 = time.perf_counter()
    registry = _ProductUrlRegistry(max_products)
    checked: list[str] = []
    errors: list[str] = []
    sitemap_files: list[dict[str, Any]] = []
    nested_fetched = 0

    def _emit(phase: str) -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "current_phase": phase,
                "sitemap_files_checked": len(checked),
                "product_urls_found": len(registry.products),
            },
        )

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        queue: deque[tuple[str, int]] = deque((u, 0) for u in sitemap_urls)
        seen_sitemaps: set[str] = set()
        _emit("reading_sitemap_index")

        while queue and (max_products is None or len(registry.products) < max_products):
            sitemap_url, depth = queue.popleft()
            if sitemap_url in seen_sitemaps or depth > _MAX_SITEMAP_DEPTH:
                continue
            seen_sitemaps.add(sitemap_url)
            checked.append(sitemap_url)

            body = await _fetch_sitemap(client, sitemap_url)
            if body is None:
                errors.append(f"Could not fetch sitemap: {sitemap_url}")
                continue

            page_locs, nested = parse_sitemap_locs(body)
            product_candidates = _product_candidates_from_locs(page_locs)
            before = len(registry.products)
            for loc in page_locs:
                registry.register(loc)
                if max_products is not None and len(registry.products) >= max_products:
                    break

            file_diag: dict[str, Any] = {
                "sitemap_url": sitemap_url,
                "loc_count": len(page_locs),
                "nested_loc_count": len(nested),
                "product_candidate_count": len(product_candidates),
                "products_added_from_file": len(registry.products) - before,
                "is_product_sitemap": _is_product_sitemap_url(sitemap_url),
                "sample_loc_values": page_locs[:5],
                "sample_product_urls": product_candidates[:5],
            }
            sitemap_files.append(file_diag)
            _log_sitemap_file_diag(file_diag)
            phase = "parsing_product_sitemaps" if file_diag["is_product_sitemap"] else "reading_sitemap_index"
            _emit(phase)

            if file_diag["loc_count"] > 0 and file_diag["product_candidate_count"] == 0:
                sample = page_locs[:10]
                errors.append(
                    f"{sitemap_url}: {file_diag['loc_count']} loc(s) but 0 product URLs; "
                    f"sample locs: {sample}",
                )

            for loc in nested:
                resolved = _resolve_sitemap_loc(sitemap_url, loc)
                if not _same_domain(resolved):
                    continue
                if resolved in seen_sitemaps:
                    continue
                if _looks_like_sitemap_url(resolved) or _is_product_sitemap_url(resolved):
                    queue.append((resolved, depth + 1))
                    nested_fetched += 1
                elif is_technopolis_product_url(resolved):
                    registry.register(resolved)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    products = sorted(registry.products)
    diag: dict[str, Any] = {
        "source": "sitemap",
        "sitemap_urls_checked": len(checked),
        "sitemap_urls": checked,
        "sitemap_files": sitemap_files,
        "nested_sitemaps_fetched": nested_fetched,
        "product_url_count": len(products),
        "duration_ms": elapsed_ms,
        "errors": errors,
    }
    return products, diag


async def crawl_product_urls_from_listings(
    seed_urls: list[str],
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
) -> tuple[list[str], dict[str, Any]]:
    """Crawl category/listing pages with pagination and collect product URLs."""
    t0 = time.perf_counter()
    registry = _ProductUrlRegistry(max_products)
    visited: set[str] = set()
    pages_scanned = 0
    errors: list[str] = []

    seeds = [u for u in seed_urls if _is_listing_page_url(u)]
    queue: deque[str] = deque(dict.fromkeys(seeds))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = await browser.new_context(
                locale="bg-BG",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1400, "height": 900},
            )
            page = await ctx.new_page()
            try:
                while queue and pages_scanned < max_pages and (
                    max_products is None or len(registry.products) < max_products
                ):
                    page_url = queue.popleft()
                    norm_page = page_url.split("#")[0].strip()
                    if norm_page in visited:
                        continue
                    if not _same_domain(norm_page):
                        continue
                    if is_excluded_url(norm_page) or is_technopolis_product_url(norm_page):
                        continue
                    visited.add(norm_page)

                    try:
                        await page.goto(norm_page, wait_until="domcontentloaded", timeout=90_000)
                        await asyncio.sleep(1.0)
                        html = await page.content()
                        pages_scanned += 1

                        for raw in extract_product_urls_from_html(html, norm_page):
                            registry.register(raw)
                            if max_products is not None and len(registry.products) >= max_products:
                                break

                        if pages_scanned < max_pages and (
                            max_products is None or len(registry.products) < max_products
                        ):
                            nxt = find_next_page_url(html, norm_page)
                            if nxt and nxt.split("#")[0] not in visited:
                                queue.append(nxt)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{norm_page}: {exc}")
            finally:
                await ctx.close()
        finally:
            await browser.close()

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    diag: dict[str, Any] = {
        "source": "crawl",
        "pages_scanned": pages_scanned,
        "listing_seeds": len(seeds),
        "visited_pages": len(visited),
        "product_url_count": len(registry.products),
        "duration_ms": elapsed_ms,
        "errors": errors[:50],
    }
    return sorted(registry.products), diag


async def discover_all_technopolis_product_urls(
    *,
    start_url: str = TECHNOPOLIS_DEFAULT_START_URL,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    sitemap_progress_callback: Callable[[dict[str, Any]], None] | None = None,
    sitemap_only: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    """
    Full-domain Technopolis product URL discovery.

    Strategy A: sitemaps. Strategy B: category/listing crawl when sitemap yields no products.
    """
    t0 = time.perf_counter()
    errors: list[str] = []

    sitemap_urls, sitemap_diag = await collect_product_urls_from_sitemaps(
        max_products=max_products,
        progress_callback=sitemap_progress_callback,
    )
    product_set = set(sitemap_urls)
    pages_scanned = 0
    crawl_diag: dict[str, Any] = {}

    if not product_set and not sitemap_only:
        try:
            nodes, cat_diag = await discover_technopolis_category_nodes(start_url=start_url)
            seed_urls = [start_url] + [n.url for n in nodes]
            crawl_urls, crawl_diag = await crawl_product_urls_from_listings(
                seed_urls,
                max_pages=max_pages,
                max_products=max_products,
            )
            product_set = set(crawl_urls)
            pages_scanned = int(crawl_diag.get("pages_scanned") or 0)
            errors.extend(crawl_diag.get("errors") or [])
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            cat_diag = {"error": str(exc)}
    else:
        cat_diag = {}

    source = "sitemap" if product_set and not crawl_diag else "crawl" if crawl_diag else "sitemap"

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    products = sorted(product_set)
    if max_products is not None:
        products = products[:max_products]
    errors = list(sitemap_diag.get("errors") or []) + errors

    diag: dict[str, Any] = {
        "source": source,
        "pages_scanned": pages_scanned,
        "sitemap_urls_checked": sitemap_diag.get("sitemap_urls_checked", 0),
        "sitemap_urls": sitemap_diag.get("sitemap_urls", []),
        "sitemap_files": sitemap_diag.get("sitemap_files", []),
        "product_urls_found": len(products),
        "max_pages": max_pages,
        "max_products": max_products,
        "duration_ms": elapsed_ms,
        "errors": errors[:50],
        "sitemap_diag": sitemap_diag,
        "crawl_diag": crawl_diag,
        "category_diag": cat_diag,
        "sample_product_urls": products[:10],
    }
    return products, diag
