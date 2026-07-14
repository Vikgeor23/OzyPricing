"""Shared Playwright browser/context for batch Technopolis scraping."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.config import get_settings
from app.scrapers.sites.technopolis_js_extract import js_extract_script, parse_js_extract_payload

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})
_TRACKING_URL = re.compile(
    r"(google-analytics|googletagmanager|doubleclick|facebook\.net|hotjar|"
    r"clarity\.ms|segment\.io|optimizely|adservice|adsystem|analytics)",
    re.I,
)

PRICE_READY_SELECTORS = (
    '[itemprop="price"]',
    ".current-price",
    ".price",
    '[data-testid*="price"]',
)


@dataclass
class PlaywrightFetchResult:
    html: str | None = None
    js_extract: dict[str, Any] | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    timed_out: bool = False
    navigation_failed: bool = False
    error: str | None = None


class TechnopolisPlaywrightPool:
    """One browser + context reused across many PDP URLs (new page per URL)."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._nav_timeout = 10_000
        self._retry_nav_timeout = 15_000
        self._title_timeout = 2_000
        self._price_selector_timeout = 3_000

    async def __aenter__(self) -> TechnopolisPlaywrightPool:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._browser is not None:
            return
        settings = get_settings()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            locale="bg-BG",
            user_agent=_USER_AGENT,
            viewport={"width": 1365, "height": 900},
        )
        await self._context.route("**/*", self._route_request)
        self._nav_timeout = settings.scrape_navigation_timeout_ms
        self._retry_nav_timeout = settings.scrape_retry_navigation_timeout_ms
        self._title_timeout = settings.scrape_title_wait_ms
        self._price_selector_timeout = settings.scrape_price_selector_wait_ms

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _route_request(self, route: Any) -> None:
        req = route.request
        if req.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        if _TRACKING_URL.search(req.url):
            await route.abort()
            return
        await route.continue_()

    async def fetch_page_data(self, url: str, *, is_retry: bool = False) -> PlaywrightFetchResult:
        if self._context is None:
            raise RuntimeError("TechnopolisPlaywrightPool not started")

        nav_timeout = self._retry_nav_timeout if is_retry else self._nav_timeout
        diagnostics: dict[str, Any] = {
            "user_agent": _USER_AGENT,
            "wait_strategy": "domcontentloaded",
            "fetch_layer": "playwright",
            "is_retry": is_retry,
        }
        page = await self._context.new_page()
        timed_out = False
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
            except PlaywrightError as exc:
                msg = str(exc).lower()
                if "timeout" in msg:
                    timed_out = True
                    diagnostics["playwright_timed_out"] = True
                    return PlaywrightFetchResult(
                        diagnostics=diagnostics,
                        timed_out=True,
                        error=str(exc),
                    )
                diagnostics["navigation_error"] = str(exc)
                return PlaywrightFetchResult(
                    diagnostics=diagnostics,
                    navigation_failed=True,
                    error=str(exc),
                )

            for sel in ('h1', 'meta[property="og:title"]'):
                try:
                    await page.wait_for_selector(sel, state="attached", timeout=self._title_timeout)
                    diagnostics["title_selector_seen"] = sel
                    break
                except PlaywrightError:
                    continue

            price_sel = await self._wait_for_price_selector(page)
            if price_sel:
                diagnostics["price_selector_seen"] = price_sel

            js_raw: Any = None
            try:
                js_text = await page.evaluate(js_extract_script())
                js_raw = json.loads(js_text) if js_text else None
            except Exception as exc:  # noqa: BLE001
                diagnostics["js_extract_error"] = str(exc)

            js_extract = js_raw if isinstance(js_raw, dict) else None
            parse_mode = "js_evaluate"
            html: str | None = None

            if js_extract and js_extract.get("priceText"):
                diagnostics["parse_mode"] = parse_mode
                return PlaywrightFetchResult(
                    html=None,
                    js_extract=js_extract,
                    diagnostics=diagnostics,
                )

            html = await page.content()
            diagnostics["parse_mode"] = "full_html"
            return PlaywrightFetchResult(html=html, js_extract=js_extract, diagnostics=diagnostics)
        except PlaywrightError as exc:
            msg = str(exc).lower()
            if "timeout" in msg:
                timed_out = True
                diagnostics["playwright_timed_out"] = True
            return PlaywrightFetchResult(
                diagnostics=diagnostics,
                timed_out=timed_out,
                error=str(exc),
            )
        finally:
            await page.close()

    async def _wait_for_price_selector(self, page: Any) -> str | None:
        for sel in PRICE_READY_SELECTORS:
            try:
                await page.wait_for_selector(sel, state="attached", timeout=self._price_selector_timeout)
                return sel
            except PlaywrightError:
                continue
        return None

    async def fetch_html(self, url: str, *, is_retry: bool = False) -> tuple[str, dict[str, Any]]:
        """Backward-compatible wrapper returning HTML + diagnostics."""
        result = await self.fetch_page_data(url, is_retry=is_retry)
        if result.timed_out:
            raise PlaywrightError(result.error or "Playwright navigation timeout")
        if result.html is None:
            raise PlaywrightError(result.error or "Playwright fetch produced no HTML")
        return result.html, result.diagnostics
