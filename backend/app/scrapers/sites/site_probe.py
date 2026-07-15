"""Cheap pre-discovery probe: inspect an unknown shop and rank discovery methods.

Runs ~10-20 short HTTP checks (no browser) to figure out what the site exposes
(product sitemaps, platform APIs, merchant feeds, autocomplete endpoints) and
returns an ordered list of discovery methods, best first, for the auto mode.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.scrapers.sites.generic_discovery import (
    _AUTOCOMPLETE_ENDPOINT_TEMPLATES,
    _BROWSER_UA,
    _CHALLENGE_MARKERS,
    _MERCHANT_FEED_PATHS,
    _looks_like_sitemap_url,
    parse_sitemap_locs,
    probe_magento_graphql,
)
from app.utils.url_utils import normalize_domain, normalize_url

_PROBE_TIMEOUT = 12.0

METHOD_SITEMAP = "sitemap"
METHOD_CATEGORY_PAGINATION = "category_pagination"
METHOD_EXTERNAL_SEARCH = "external_search"
METHOD_DYNAMIC_ENDPOINTS = "dynamic_endpoints"
METHOD_SITE_SEARCH = "site_search"
METHOD_MERCHANT_FEEDS = "merchant_feeds"
METHOD_AUTOCOMPLETE = "autocomplete"


def _headers() -> dict[str, str]:
    return {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        return await client.get(url, follow_redirects=True)
    except Exception:  # noqa: BLE001
        return None


async def _probe_sitemap(client: httpx.AsyncClient, origin: str) -> dict[str, Any]:
    """Locate a sitemap and judge its quality (product sitemap? how many locs?)."""
    out: dict[str, Any] = {
        "found": False,
        "sitemap_url": None,
        "product_sitemap": False,
        "sample_loc_count": 0,
    }
    candidates: list[str] = []
    resp = await _get(client, f"{origin}/robots.txt")
    if resp is not None and resp.status_code == 200:
        for line in resp.text.splitlines():
            if line.lower().startswith("sitemap:"):
                raw = line.split(":", 1)[1].strip()
                if raw:
                    candidates.append(raw)
    candidates.extend(
        [
            f"{origin}/sitemap.xml",
            f"{origin}/sitemap_index.xml",
            f"{origin}/wp-sitemap.xml",
            f"{origin}/sitemap_products_1.xml",
        ],
    )
    seen: set[str] = set()
    for candidate in candidates[:6]:
        if candidate in seen:
            continue
        seen.add(candidate)
        resp = await _get(client, candidate)
        if resp is None or resp.status_code != 200:
            continue
        page_locs, nested = parse_sitemap_locs(resp.content)
        if not page_locs and not nested:
            continue
        out["found"] = True
        out["sitemap_url"] = candidate
        out["sample_loc_count"] = len(page_locs)
        product_named = [
            u for u in nested if any(k in u.lower() for k in ("product", "prod", "offer", "detail", "/pdp"))
        ]
        if product_named or any(k in candidate.lower() for k in ("product", "detail")):
            out["product_sitemap"] = True
        return out
    return out


async def _probe_platform(client: httpx.AsyncClient, origin: str, home_html: str) -> dict[str, Any]:
    out: dict[str, Any] = {"platform": None, "api": None}
    resp = await _get(client, f"{origin}/products.json?limit=1")
    if resp is not None and resp.status_code == 200:
        try:
            if isinstance(resp.json().get("products"), list):
                out["platform"] = "shopify"
                out["api"] = "/products.json"
                return out
        except (json.JSONDecodeError, AttributeError):
            pass
    resp = await _get(client, f"{origin}/wp-json/wc/store/products?per_page=1")
    if resp is not None and resp.status_code == 200:
        try:
            if isinstance(resp.json(), list):
                out["platform"] = "woocommerce"
                out["api"] = "/wp-json/wc/store/products"
                return out
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    if await probe_magento_graphql(client, origin):
        out["platform"] = "magento"
        out["api"] = "/graphql"
        return out
    low = home_html[:200_000].lower()
    if "route=product" in low or "route=common" in low:
        out["platform"] = "opencart"
    elif "/wp-content/" in low:
        out["platform"] = "wordpress"
    return out


async def _probe_merchant_feed(client: httpx.AsyncClient, origin: str) -> dict[str, Any]:
    for feed_path in _MERCHANT_FEED_PATHS[:8]:
        feed_url = urljoin(origin, feed_path)
        resp = await _get(client, feed_url)
        if resp is None or resp.status_code != 200:
            continue
        body = resp.content[:200_000]
        low = body.decode("utf-8", errors="ignore").lower()
        if "<item" in low or ("<url" in low and not _looks_like_sitemap_url(feed_url)):
            return {"found": True, "feed_url": feed_url}
    return {"found": False, "feed_url": None}


async def _probe_autocomplete(client: httpx.AsyncClient, origin: str) -> dict[str, Any]:
    from urllib.parse import quote

    for template in _AUTOCOMPLETE_ENDPOINT_TEMPLATES:
        endpoint = urljoin(origin, template.format(q=quote("a")))
        resp = await _get(client, endpoint)
        if resp is None or resp.status_code != 200:
            continue
        text = resp.text.lstrip()
        if text.startswith(("{", "[")) and len(text) > 2:
            return {"found": True, "endpoint": template}
    return {"found": False, "endpoint": None}


# Subdomain labels that are infrastructure/support, never a product catalogue —
# excluded from the "found subdomains" the user can opt into.
_NON_SHOP_SUBDOMAINS = {
    "www", "m", "help", "support", "blog", "mail", "webmail", "smtp", "cdn",
    "static", "img", "images", "assets", "api", "account", "accounts", "login",
    "admin", "status", "docs", "kb", "forum", "news", "careers", "jobs", "press",
    "media", "ads", "track", "analytics", "affiliate",
}


def _detect_subdomains(home_html: str, *, domain: str) -> list[dict[str, Any]]:
    """Find sibling subdomains linked from the homepage (multi-subdomain shops).

    Returns ``[{"host": ..., "links": N}, ...]`` sorted by link count, excluding
    the bare domain, ``www.``, non-shop infrastructure subdomains and foreign
    domains. Detection only — the user chooses which to actually crawl.
    """
    base = normalize_domain(domain)
    if not base or not home_html:
        return []
    counts: Counter[str] = Counter()
    suffix = "." + base
    for match in re.finditer(r"""href=["']([^"'>\s]+)""", home_html):
        host = urlparse(match.group(1)).netloc.lower().split(":")[0]
        if not host or not host.endswith(suffix):
            continue
        labels = host[: -len(suffix)].split(".")
        if labels and labels[0] == "www":
            labels = labels[1:]
        if not labels or labels[0] in _NON_SHOP_SUBDOMAINS:
            continue
        counts[".".join(labels) + suffix] += 1
    return [{"host": host, "links": links} for host, links in counts.most_common()]


async def probe_site(site_url_or_domain: str) -> dict[str, Any]:
    """Inspect the shop and return ranked discovery methods with reasons."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    parsed = urlparse(start)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    domain = normalize_domain(start)

    signals: dict[str, Any] = {}
    blocked = False
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, headers=_headers()) as client:
        home = await _get(client, origin)
        home_html = home.text if home is not None and home.status_code == 200 else ""
        if home is not None and home.status_code in (401, 403, 429):
            blocked = True
        if home_html and any(marker in home_html[:5000].lower() for marker in _CHALLENGE_MARKERS):
            blocked = True
        signals["home_status"] = home.status_code if home is not None else None
        signals["blocked"] = blocked

        signals["sitemap"] = await _probe_sitemap(client, origin)
        signals["platform"] = await _probe_platform(client, origin, home_html)
        signals["merchant_feed"] = await _probe_merchant_feed(client, origin)
        signals["autocomplete"] = await _probe_autocomplete(client, origin)

    scores: dict[str, tuple[int, str]] = {}
    sitemap_sig = signals["sitemap"]
    platform_sig = signals["platform"]

    if sitemap_sig["product_sitemap"]:
        scores[METHOD_SITEMAP] = (95, "dedicated product sitemap found")
    elif sitemap_sig["found"]:
        scores[METHOD_SITEMAP] = (70, f"sitemap found ({sitemap_sig['sample_loc_count']} entries sampled)")

    if platform_sig["platform"] in ("shopify", "woocommerce", "magento"):
        scores[METHOD_DYNAMIC_ENDPOINTS] = (
            90,
            f"{platform_sig['platform']} catalog API detected at {platform_sig['api']}",
        )

    if signals["merchant_feed"]["found"]:
        scores[METHOD_MERCHANT_FEEDS] = (80, f"merchant feed at {signals['merchant_feed']['feed_url']}")

    if signals["autocomplete"]["found"]:
        scores[METHOD_AUTOCOMPLETE] = (55, f"autocomplete endpoint {signals['autocomplete']['endpoint']}")

    # Always-available fallbacks, ranked below detected signals.
    scores.setdefault(METHOD_CATEGORY_PAGINATION, (40, "fallback: crawl categories and pagination"))
    scores.setdefault(METHOD_EXTERNAL_SEARCH, (30, "fallback: external indexes (Common Crawl, Wayback)"))
    scores.setdefault(METHOD_SITE_SEARCH, (20, "fallback: on-site search with seed terms"))
    scores.setdefault(METHOD_DYNAMIC_ENDPOINTS, (25, "fallback: probe JSON endpoints and XHR traffic"))
    scores.setdefault(METHOD_SITEMAP, (15, "fallback: no sitemap detected during probe"))

    ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
    return {
        "domain": domain,
        "origin": origin,
        "blocked": blocked,
        "platform": platform_sig["platform"],
        "signals": signals,
        "recommended_methods": [method for method, _ in ranked],
        "method_reasons": {method: reason for method, (_, reason) in ranked},
        "method_scores": {method: score for method, (score, _) in ranked},
        "best_method": ranked[0][0],
        "detected_subdomains": _detect_subdomains(home_html, domain=domain),
        "duration_ms": int((time.perf_counter() - t0) * 1000),
    }
