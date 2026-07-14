"""Technopolis.bg — discover product detail URLs on a category/listing page (no price scraping)."""

from __future__ import annotations

import asyncio
import unicodedata
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from app.scrapers.sites.technopolis_categories import is_technopolis_url
from app.scrapers.sites.technopolis_urls import is_technopolis_product_detail_url


def _norm_abs(base: str, href: str) -> str | None:
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        return None
    u = unicodedata.normalize("NFKC", urljoin(base, href)).strip()
    u = u.split("#")[0]
    return u


def is_product_detail_url(url: str) -> bool:
    if not is_technopolis_url(url):
        return False
    return is_technopolis_product_detail_url(url)


def extract_product_urls_from_html(html: str, page_url: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    out: set[str] = set()
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        abs_u = _norm_abs(page_url, href)
        if abs_u and is_product_detail_url(abs_u):
            out.add(abs_u)
    return out


def find_next_page_url(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for sel in (
        'a[rel="next"]',
        "a.pagination__next",
        "a.next",
        "a[aria-label='Next']",
        "a[aria-label='Следваща']",
    ):
        for a in soup.select(sel):
            href = a.get("href")
            nu = _norm_abs(page_url, href or "")
            if nu:
                candidates.append(nu)
    for a in soup.select("a[href*='page=']"):
        nu = _norm_abs(page_url, a.get("href") or "")
        if nu and nu != page_url:
            candidates.append(nu)
    if not candidates:
        return None
    # Prefer explicit rel=next
    for c in candidates:
        if "page=" in c or "/page/" in c.lower():
            return c
    return candidates[0]


async def discover_product_urls_for_category(
    category_url: str,
    *,
    max_pages: int = 5,
    max_products: int = 200,
) -> tuple[list[str], dict[str, Any]]:
    """Walk up to ``max_pages`` listing pages and collect product URLs (dedup, cap)."""
    import time

    t0 = time.perf_counter()
    seen_pages: set[str] = set()
    products: list[str] = []
    products_set: set[str] = set()
    page_url = category_url
    pages_fetched = 0
    next_url: str | None = category_url

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
                while next_url and pages_fetched < max_pages and len(products_set) < max_products:
                    if next_url in seen_pages:
                        break
                    seen_pages.add(next_url)
                    await page.goto(next_url, wait_until="domcontentloaded", timeout=90_000)
                    await asyncio.sleep(1.5)
                    html = await page.content()
                    pages_fetched += 1
                    for u in extract_product_urls_from_html(html, next_url):
                        if len(products_set) >= max_products:
                            break
                        if u not in products_set:
                            products_set.add(u)
                            products.append(u)
                    if len(products_set) >= max_products:
                        break
                    nxt = find_next_page_url(html, next_url)
                    next_url = nxt if nxt and nxt not in seen_pages else None
            finally:
                await ctx.close()
        finally:
            await browser.close()

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    diag: dict[str, Any] = {
        "source": "technopolis_discovery",
        "category_url": category_url,
        "pages_fetched": pages_fetched,
        "product_url_count": len(products_set),
        "duration_ms": elapsed_ms,
        "max_pages": max_pages,
        "max_products": max_products,
    }
    return products[:max_products], diag
