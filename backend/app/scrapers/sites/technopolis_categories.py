"""Technopolis.bg — discover category navigation links from the home page (depth-limited).

Does not crawl the entire site: starts from ``https://www.technopolis.bg/bg/``, collects in-page
links suitable for catalog browsing, deduplicates URLs, infers a parent/child tree from URL
prefixes, and caps folder depth at **3** below ``/bg/``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from app.utils.url_utils import TECHNOPOLIS_DEFAULT_START_URL, is_technopolis, normalize_domain

_EXCLUDE_FRAGMENTS = (
    "/cart",
    "/kosnica",
    "/account",
    "/login",
    "/register",
    "/user/",
    "/search",
    "/compare",
    "/wishlist",
    "/checkout",
    "/stores",
    "/help",
    "/privacy",
    "/terms",
    "/favorites",
    "/favorite",
    "/static/",
    "/promo",
    "/promotion",
    "/top5",
    "/top-5",
    "/leasing",
    "/weekly",
    "/offers",
    "javascript:",
    "mailto:",
    "#",
)

_NAV_SELECTORS = (
    "nav a[href]",
    "header nav a[href]",
    ".navigation a[href]",
    ".main-navigation a[href]",
    "[class*='main-menu'] a[href]",
    "[class*='nav-menu'] a[href]",
)

_EXCLUDED_NAME_PATTERNS = (
    re.compile(r"top5\s*list", re.IGNORECASE),
    re.compile(r"^promotions?$", re.IGNORECASE),
    re.compile(r"weekly\s*offers?", re.IGNORECASE),
    re.compile(r"zero\s*leasing", re.IGNORECASE),
    re.compile(r"^p\d+$", re.IGNORECASE),
)

_CATEGORY_SUFFIX_HINTS = ("/c/", "/category/", "/catalog/", "/kategorii/")


def _norm_host(host: str) -> str:
    return normalize_domain(host)


def is_technopolis_url(url: str) -> bool:
    return is_technopolis(url)


def _path_segments(url: str) -> list[str]:
    path = urlparse(url).path.strip("/")
    return [s for s in path.split("/") if s]


def _slug_to_name(slug: str) -> str:
    t = slug.replace("-", " ").replace("_", " ").strip()
    return re.sub(r"\s+", " ", t).title() or slug


def is_product_like_url(url: str) -> bool:
    """Technopolis PDP URLs: ``/p/{code}`` or legacy ``-digits.html``."""
    from app.scrapers.sites.technopolis_urls import is_technopolis_product_detail_url

    return is_technopolis_product_detail_url(url)


def normalize_category_name(name: str) -> str:
    """Trim, collapse whitespace, strip control chars; keep Bulgarian text as-is."""
    if not name:
        return ""
    t = unicodedata.normalize("NFKC", name)
    t = re.sub(r"[\x00-\x1f\x7f]+", " ", t)
    t = re.sub(r"\s+", " ", t.strip())
    return t[:512]


def is_excluded_category_name(name: str) -> bool:
    normalized = normalize_category_name(name)
    if not normalized or len(normalized) < 2:
        return True
    for pat in _EXCLUDED_NAME_PATTERNS:
        if pat.search(normalized):
            return True
    # Slug-like internal codes when used as visible label
    if re.fullmatch(r"p\d+", normalized, re.IGNORECASE):
        return True
    return False


def is_excluded_category_slug(slug: str) -> bool:
    if not slug:
        return True
    if re.fullmatch(r"p\d+", slug, re.IGNORECASE):
        return True
    low = slug.lower()
    for bad in ("top5", "top-5", "promo", "promotion", "leasing", "weekly", "offer", "zero-leasing"):
        if bad in low:
            return True
    return False


def is_excluded_url(url: str) -> bool:
    low = url.lower()
    for frag in _EXCLUDE_FRAGMENTS:
        if frag in low:
            return True
    try:
        qs = parse_qs(urlparse(url).query)
        if qs.get("promo") or qs.get("promotion"):
            return True
    except Exception:
        pass
    return False


def is_category_candidate_url(url: str) -> bool:
    if not is_technopolis_url(url):
        return False
    if is_excluded_url(url):
        return False
    if is_product_like_url(url):
        return False
    segs = _path_segments(url)
    if not segs or segs[0].lower() != "bg":
        return False
    depth_under_bg = len(segs) - 1
    if depth_under_bg < 1 or depth_under_bg > 3:
        return False
    lowp = urlparse(url).path.lower()
    if any(x in lowp for x in ("/product/", "/p/", "/product.jsp", "/item/")):
        return False
    if segs[-1] and is_excluded_category_slug(segs[-1]):
        return False
    # Prefer Technopolis catalog URLs with /c/ segment
    if "/c/" in lowp:
        return True
    # Allow readable category slugs (Latin or Cyrillic), not internal codes
    last = segs[-1]
    if re.fullmatch(r"p\d+", last, re.IGNORECASE):
        return False
    if depth_under_bg >= 2 and re.search(r"[a-zA-Zа-яА-ЯёЁ0-9]", last):
        if any(h in lowp for h in _CATEGORY_SUFFIX_HINTS):
            return True
        # Multi-segment paths like /bg/telefoni/smartfoni/
        if re.search(r"[a-zA-Zа-яА-Я]", last) and len(last) > 2:
            return True
    return False


def _normalize_catalog_url(url: str) -> str:
    p = urlparse(url)
    clean = p._replace(fragment="", query="")
    return clean.geturl()


@dataclass(frozen=True)
class CategoryNode:
    name: str
    url: str
    url_key: str
    parent_url_key: str | None
    level: int
    in_nav: bool = False


@dataclass
class CategoryLinkCandidate:
    url: str
    name: str
    in_nav: bool = False


def _dedupe_nodes(nodes: list[CategoryNode]) -> list[CategoryNode]:
    by_key: dict[str, CategoryNode] = {}
    for n in sorted(nodes, key=lambda x: len(x.url_key)):
        if n.url_key not in by_key:
            by_key[n.url_key] = n
    return list(by_key.values())


def build_nodes_from_urls(
    urls: set[str],
    *,
    link_meta: dict[str, CategoryLinkCandidate] | None = None,
) -> list[CategoryNode]:
    """Infer tree from URL prefixes; prefer anchor link text for display names."""
    meta = link_meta or {}
    norm_urls = {_normalize_catalog_url(u) for u in urls if is_category_candidate_url(u)}
    keys: dict[str, str] = {}
    key_to_display: dict[str, str] = {}
    key_in_nav: dict[str, bool] = {}

    for u in norm_urls:
        segs_u = _path_segments(u)
        pk = "/" + "/".join(segs_u) + "/" if segs_u else "/"
        keys[pk] = u
        cand = meta.get(u) or meta.get(u.rstrip("/"))
        if cand and cand.name and not is_excluded_category_name(cand.name):
            display = normalize_category_name(cand.name)
        else:
            display = _slug_to_name(segs_u[-1]) if segs_u else u
        key_to_display[pk] = display
        key_in_nav[pk] = bool(cand.in_nav if cand else False)

    path_keys = sorted(keys.keys(), key=len)

    nodes: list[CategoryNode] = []
    for pk in path_keys:
        url = keys[pk]
        segs = _path_segments(url)
        display_name = normalize_category_name(key_to_display.get(pk) or "")
        if is_excluded_category_name(display_name):
            continue

        level = max(0, len(segs) - 2)
        parent_key: str | None = None
        if len(segs) > 2:
            best = ""
            for i in range(len(segs) - 1, 1, -1):
                cand = "/" + "/".join(segs[:i]) + "/"
                if cand in keys and cand != pk:
                    best = cand
                    break
            parent_key = best or None

        nodes.append(
            CategoryNode(
                name=display_name[:512],
                url=url,
                url_key=pk,
                parent_url_key=parent_key,
                level=min(level, 3),
                in_nav=key_in_nav.get(pk, False),
            ),
        )
    return filter_category_nodes(_dedupe_nodes(nodes))


def filter_category_nodes(nodes: list[CategoryNode]) -> list[CategoryNode]:
    """Drop promotional/menu noise; dedupe labels; prefer /c/ and nav links."""
    if not nodes:
        return []

    scored: list[tuple[int, CategoryNode]] = []
    for n in nodes:
        if is_excluded_category_name(n.name):
            continue
        segs = _path_segments(n.url)
        if segs and is_excluded_category_slug(segs[-1]):
            continue
        score = 0
        if "/c/" in n.url.lower():
            score += 10
        if n.in_nav:
            score += 5
        if re.search(r"[а-яА-Я]", n.name):
            score += 2
        scored.append((score, n))

    # Dedupe by normalized name — keep highest score
    by_name: dict[str, tuple[int, CategoryNode]] = {}
    for score, n in scored:
        key = normalize_category_name(n.name).lower()
        prev = by_name.get(key)
        if prev is None or score > prev[0]:
            by_name[key] = (score, n)

    kept = {n.url_key: n for _, n in by_name.values()}
    # Drop orphans whose parent was filtered out (re-parent to None)
    out: list[CategoryNode] = []
    for n in kept.values():
        parent = n.parent_url_key if n.parent_url_key in kept else None
        out.append(
            CategoryNode(
                name=n.name,
                url=n.url,
                url_key=n.url_key,
                parent_url_key=parent,
                level=n.level,
                in_nav=n.in_nav,
            ),
        )
    return sorted(out, key=lambda x: (x.level, x.name.lower()))


async def fetch_page_html(start_url: str) -> str:
    """Load a catalog page HTML with Playwright."""
    from playwright.async_api import async_playwright

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
                await page.goto(
                    start_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                await asyncio.sleep(2.5)
                return await page.content()
            finally:
                await ctx.close()
        finally:
            await browser.close()


async def fetch_home_html() -> str:
    """Load Technopolis home page HTML (default start URL)."""
    return await fetch_page_html(TECHNOPOLIS_DEFAULT_START_URL)


def collect_category_links_from_html(
    html: str,
    base_url: str = TECHNOPOLIS_DEFAULT_START_URL,
) -> dict[str, CategoryLinkCandidate]:
    """Collect category URLs with anchor text; mark main-navigation links."""
    soup = BeautifulSoup(html, "html.parser")
    nav_urls: set[str] = set()
    for sel in _NAV_SELECTORS:
        for a in soup.select(sel):
            href = a.get("href") or ""
            if not href or href.startswith("#"):
                continue
            abs_url = urljoin(base_url, href).split("#")[0]
            abs_url = unicodedata.normalize("NFKC", abs_url).strip()
            if is_category_candidate_url(abs_url):
                nav_urls.add(_normalize_catalog_url(abs_url))

    out: dict[str, CategoryLinkCandidate] = {}
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if not href or href.startswith("#"):
            continue
        abs_url = urljoin(base_url, href)
        abs_url = unicodedata.normalize("NFKC", abs_url).strip().split("#")[0]
        if not is_category_candidate_url(abs_url):
            continue
        norm = _normalize_catalog_url(abs_url)
        link_text = normalize_category_name(a.get_text(" ", strip=True))
        in_nav = norm in nav_urls
        prev = out.get(norm)
        if prev is None:
            out[norm] = CategoryLinkCandidate(url=norm, name=link_text, in_nav=in_nav)
        else:
            # Prefer longer meaningful label; keep nav flag
            name = link_text if len(link_text) > len(prev.name) else prev.name
            out[norm] = CategoryLinkCandidate(url=norm, name=name, in_nav=prev.in_nav or in_nav)
    return out


def collect_candidate_urls_from_html(html: str, base_url: str = TECHNOPOLIS_DEFAULT_START_URL) -> set[str]:
    return set(collect_category_links_from_html(html, base_url).keys())


async def discover_technopolis_category_nodes(
    *,
    start_url: str | None = None,
) -> tuple[list[CategoryNode], dict[str, Any]]:
    """Return flat ``CategoryNode`` list plus diagnostics for logging / raw payloads."""
    import time

    entry_url = start_url or TECHNOPOLIS_DEFAULT_START_URL
    t0 = time.perf_counter()
    html = await fetch_page_html(entry_url)
    link_meta = collect_category_links_from_html(html, entry_url)
    found = set(link_meta.keys())
    nodes = build_nodes_from_urls(found, link_meta=link_meta)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    diag: dict[str, Any] = {
        "source": "technopolis_categories",
        "home": entry_url,
        "raw_link_count": len(found),
        "category_node_count": len(nodes),
        "duration_ms": elapsed_ms,
    }
    return nodes, diag
