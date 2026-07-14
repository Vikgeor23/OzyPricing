"""Technopolis Bulgaria — product detail pages only (Playwright + BS4)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.config import get_settings
from app.scrapers.base import BaseScraper, ScrapeResult
from app.scrapers.sites.technopolis_breadcrumbs import extract_breadcrumb_categories
from app.scrapers.sites.technopolis_specs import extract_technopolis_product_specs
from app.scrapers.sites.technopolis_urls import extract_url_metadata
from app.scrapers.bg_price import (
    extract_all_leva_amounts,
    parse_bg_leva_amount,
    pick_likely_prices,
)
from app.utils.url_utils import is_technopolis

logger = logging.getLogger(__name__)

SITE_SLUG = "technopolis_bg"

TITLE_SELECTORS = [
    "h1",
    '[itemprop="name"]',
    "h1.product-title",
    ".product-title",
    ".pdp-product-title",
    'meta[property="og:title"]',
    "title",
]

PRICE_CONTAINER_SELECTORS = [
    '[itemprop="price"]',
    '[data-price]',
    ".current-price",
    ".product-price",
    ".price-box .price",
    ".price",
    "span[class*='price']",
    "div[class*='price']",
]

STRIKE_SELECTORS = ["s", "del", "strike", ".old-price", ".previous-price", '[class*="old"]', '[class*="strike"]']

IMAGE_SELECTORS = [
    'meta[property="og:image"]',
    'link[rel="image_src"]',
    "img.product-image",
    "img[class*='product']",
    ".product-gallery img",
    "picture img",
]

STOCK_PATTERNS = re.compile(
    r"(в\s+наличност|изчерпан|заявка|очакваме|не\s+е\s+наличен|on\s+order|out\s+of\s+stock)",
    re.IGNORECASE,
)

FAILURE_DIR = Path(__file__).resolve().parents[3] / "storage" / "scrape_failures"


class TechnopolisFetchError(Exception):
    """Raised when navigation or render fails; may include a debug screenshot path."""

    def __init__(self, cause: BaseException, screenshot_path: str | None = None) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.screenshot_path = screenshot_path


def _mkdir_failure_storage() -> Path:
    FAILURE_DIR.mkdir(parents=True, exist_ok=True)
    return FAILURE_DIR


class TechnopolisScraper(BaseScraper):
    """Render PDP with Playwright, extract prices with selectors + BG text fallback."""

    navigation_timeout_ms = 15_000
    post_goto_wait_ms = 500

    async def fetch(self) -> str:
        html, _ = await self._fetch_html_with_page()
        return html

    def parse(self, raw: str) -> ScrapeResult:
        return self._parse_html_to_result(
            raw,
            extra_raw={},
            captured_at=datetime.now(timezone.utc),
        )

    async def run(self) -> ScrapeResult:
        """HTTP-first hybrid scrape; Playwright only on fallback (never raises)."""
        from app.scrapers.sites.technopolis_hybrid import scrape_technopolis_url

        return await scrape_technopolis_url(self.listing_url, pool=None)

    def _failure_result(
        self,
        started: datetime,
        duration_ms: int,
        exc: BaseException,
        *,
        screenshot_path: str | None,
        raw_data: dict[str, Any] | None = None,
    ) -> ScrapeResult:
        from app.services.scrape_errors import classify_scrape_failure

        payload: dict[str, Any] = {
            "scraper_status": "failure",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "duration_ms": duration_ms,
            "scrape_timestamp": started.isoformat(),
            "url": self.listing_url,
            "extracted_selectors": {},
            "selectors": {},
            **(raw_data or {}),
        }
        payload.setdefault(
            "scrape_error_code",
            classify_scrape_failure(exc=exc, error_message=str(exc), raw_data=payload),
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

    async def _fetch_html_with_page(self) -> tuple[str, dict[str, Any]]:
        diagnostics: dict[str, Any] = {
            "user_agent": None,
            "wait_strategy": None,
            "title_selector_seen": None,
            "price_selector_seen": None,
        }

        settings = get_settings()
        nav_timeout = settings.scrape_navigation_timeout_ms
        sel_timeout = settings.scrape_selector_timeout_ms

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
                    viewport={"width": 1365, "height": 900},
                )
                from app.scrapers.sites.technopolis_playwright_pool import TechnopolisPlaywrightPool

                _pool = TechnopolisPlaywrightPool()
                await ctx.route("**/*", _pool._route_request)
                page = await ctx.new_page()
                diagnostics["user_agent"] = await page.evaluate("navigator.userAgent")
                try:
                    await self._goto_with_fallback(page, diagnostics)
                    await self._wait_for_product_signals(page, diagnostics)
                    await asyncio.sleep(self.post_goto_wait_ms / 1000)
                    html = await page.content()
                    return html, diagnostics
                except Exception as exc:
                    shot: str | None = None
                    try:
                        shot = await self._screenshot_page_failure(page)
                    except Exception:
                        logger.exception("screenshot during fetch failure")
                    raise TechnopolisFetchError(exc, shot) from exc
                finally:
                    await ctx.close()
            finally:
                await browser.close()

    async def _goto_with_fallback(self, page: Any, diagnostics: dict[str, Any]) -> None:
        settings = get_settings()
        await page.goto(
            self.listing_url,
            wait_until="domcontentloaded",
            timeout=settings.scrape_navigation_timeout_ms,
        )
        diagnostics["wait_strategy"] = "domcontentloaded"

    async def _wait_for_product_signals(self, page: Any, diagnostics: dict[str, Any]) -> None:
        settings = get_settings()
        title_timeout = settings.scrape_title_wait_ms
        price_timeout = settings.scrape_price_wait_ms
        for sel in ('h1', 'meta[property="og:title"]'):
            try:
                await page.wait_for_selector(sel, state="attached", timeout=title_timeout)
                diagnostics["title_selector_seen"] = sel
                break
            except PlaywrightError:
                continue
        try:
            await page.wait_for_function(
                """() => document.body && /лв|BGN|€/i.test(document.body.innerText)""",
                timeout=price_timeout,
            )
            diagnostics["price_selector_seen"] = "text:лв|BGN|€ (body)"
        except PlaywrightError:
            pass

    async def _screenshot_page_failure(self, page: Any) -> str | None:
        try:
            _mkdir_failure_storage()
            name = (
                f"fail_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.png"
            )
            dest = FAILURE_DIR / name
            await page.screenshot(path=str(dest), full_page=True)
            return str(dest.resolve())
        except Exception:
            logger.exception("screenshot_on_failure failed url=%s", self.listing_url)
            return None

    def _parse_html_to_result(
        self,
        html: str,
        *,
        extra_raw: dict[str, Any],
        captured_at: datetime,
    ) -> ScrapeResult:
        soup = BeautifulSoup(html, "html.parser")

        title, title_sel, title_meta = self._extract_title(soup)
        image_url, img_sel = self._extract_image(soup, self.listing_url)

        strike_text = " ".join(self._gather_strike_texts(soup))
        main_price_text = self._gather_price_texts(soup)

        strike_amounts = extract_all_leva_amounts(strike_text)
        price_amounts_sel = self._amounts_from_price_selectors(soup)

        body_text = soup.get_text("\n", strip=True)
        text_all_leva = extract_all_leva_amounts(body_text)

        old_price, strike_dec = self._pick_strike_old(strike_amounts, strike_text)
        current_from_sel, price_sel_used = self._pick_selector_price(soup)

        parsed_lines: dict[str, Any] = {
            "title_selector": title_sel,
            "title_meta": title_meta,
            "image_selector": img_sel,
            "price_selector_trace": price_sel_used,
            "strike_sample": strike_text[:500],
            "main_price_sample": main_price_text[:500],
            "amounts_from_leva_regex": [str(x) for x in text_all_leva[:20]],
            "amounts_from_strike": [str(x) for x in strike_amounts],
        }

        price: Decimal | None = None
        promo_price: Decimal | None = None
        old_p: Decimal | None = old_price or strike_dec

        if current_from_sel is not None:
            price = current_from_sel
        elif price_amounts_sel:
            price = price_amounts_sel[0]
        elif text_all_leva:
            low, high, _ = pick_likely_prices(text_all_leva)
            price = low
            old_p = old_p or high

        if price is None and text_all_leva:
            low, high, promo = pick_likely_prices(text_all_leva)
            price = low
            old_p = old_p or high
            if promo is not None and high is not None and promo <= high:
                promo_price = promo

        if old_p is not None and price is not None and old_p <= price:
            old_p = None
        if promo_price is not None and price is not None and promo_price >= price:
            promo_price = None
        if promo_price is None and old_p is not None and price is not None and price < old_p:
            promo_price = price

        availability = self._availability(body_text)

        breadcrumb_categories = extract_breadcrumb_categories(
            soup,
            self.listing_url,
            product_title=title,
        )
        url_meta = extract_url_metadata(self.listing_url)
        product_ids = extract_technopolis_product_specs(soup, url_meta=url_meta)

        raw_data: dict[str, Any] = {
            "url": self.listing_url,
            "scrape_timestamp": captured_at.isoformat(),
            "selectors": {
                "title": title_sel,
                "image": img_sel,
                "price_trace": price_sel_used,
            },
            "extracted_selectors": parsed_lines,
            "currency_detected": "BGN",
            "breadcrumb_categories": breadcrumb_categories,
            "specs_json": product_ids.get("specs_json"),
            "raw_identifiers": product_ids.get("raw_identifiers"),
            "product_identifiers": {
                "ean": product_ids.get("ean"),
                "manufacturer_code": product_ids.get("manufacturer_code"),
                "model": product_ids.get("model"),
                "brand": product_ids.get("brand"),
            },
            **url_meta,
            **extra_raw,
        }

        return ScrapeResult(
            title=title,
            price=price,
            old_price=old_p,
            promo_price=promo_price,
            currency="BGN",
            availability=availability,
            captured_at=captured_at,
            image_url=image_url,
            raw_data=raw_data,
        )

    def _extract_title(self, soup: BeautifulSoup) -> tuple[str | None, str | None, dict[str, str]]:
        meta: dict[str, str] = {}
        for sel in TITLE_SELECTORS:
            node = soup.select_one(sel)
            if node is None:
                continue
            text = (node.get("content") if node.name == "meta" else None) or node.get_text(" ", strip=True)
            if text and len(text) > 2:
                meta["selector"] = sel
                return text[:512], sel, meta
        return None, None, meta

    def _extract_image(self, soup: BeautifulSoup, page_url: str) -> tuple[str | None, str | None]:
        for sel in IMAGE_SELECTORS:
            node = soup.select_one(sel)
            if node is None:
                continue
            href = node.get("content") or node.get("href") or node.get("src")
            if not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                pu = urlparse(page_url)
                href = f"{pu.scheme}://{pu.netloc}{href}"
            return href, sel
        return None, None

    def _gather_strike_texts(self, soup: BeautifulSoup) -> list[str]:
        texts: list[str] = []
        for sel in STRIKE_SELECTORS:
            for node in soup.select(sel):
                t = node.get_text(" ", strip=True)
                if t:
                    texts.append(t)
        return texts

    def _gather_price_texts(self, soup: BeautifulSoup) -> str:
        chunks: list[str] = []
        for sel in PRICE_CONTAINER_SELECTORS[:8]:
            for node in soup.select(sel)[:8]:
                chunks.append(node.get_text(" ", strip=True))
        return " ".join(chunks)

    def _amounts_from_price_selectors(self, soup: BeautifulSoup) -> list[Decimal]:
        out: list[Decimal] = []
        for sel in PRICE_CONTAINER_SELECTORS:
            for node in soup.select(sel):
                t = node.get_text(" ", strip=True)
                if not t:
                    at = node.get("content") or node.get("data-price") or node.get("data-value")
                    t = at or ""
                for m in re.finditer(r"([\d\s][\d\s.,]*)\s*(?:лв|BGN)", t, re.I):
                    p = parse_bg_leva_amount(m.group(1))
                    if p is not None:
                        out.append(p)
        return out

    def _pick_selector_price(self, soup: BeautifulSoup) -> tuple[Decimal | None, list[str]]:
        trace: list[str] = []
        for sel in PRICE_CONTAINER_SELECTORS:
            for node in soup.select(sel):
                t = node.get_text(" ", strip=True)
                if not t:
                    at = node.get("content") or node.get("data-price")
                    t = at or ""
                if not t:
                    continue
                for m in re.finditer(r"([\d\s][\d\s.,]*)\s*(?:лв\.?|BGN)\b", t, re.I):
                    p = parse_bg_leva_amount(m.group(1))
                    if p is not None:
                        trace.append(f"{sel}:{p}")
                        return p, trace
                p = parse_bg_leva_amount(t[:48].strip())
                if p is not None:
                    trace.append(f"{sel}:{p}")
                    return p, trace
        return None, trace

    def _pick_strike_old(self, strike_amounts: list[Decimal], strike_text: str) -> tuple[Decimal | None, Decimal | None]:
        if strike_amounts:
            mx = max(strike_amounts)
            return mx, mx
        m = re.search(r"([\d\s][\d\s.,]*)\s*(?:лв|BGN)", strike_text, re.I)
        if m:
            p = parse_bg_leva_amount(m.group(1))
            return (p, p) if p is not None else (None, None)
        return None, None

    def _availability(self, body: str) -> str | None:
        m = STOCK_PATTERNS.search(body)
        if not m:
            return "unknown"
        return m.group(1).lower().replace(" ", "_")
