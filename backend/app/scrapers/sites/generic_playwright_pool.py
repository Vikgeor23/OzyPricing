"""Shared pool of reused Chromium browsers for generic product scraping.

Launching a fresh browser per product (as the standalone ``_fetch_playwright``
path does) is fine for a single scrape but catastrophic in a batch: at
concurrency N it spawns N Chromium processes at once, and the launch storm —
not the navigation — dominates wall time.

This pool launches a small, fixed set of browsers once and reuses those heavy
processes, but hands out a **fresh context per fetch** (round-robin over the
browsers). The fresh context matters: anti-bot layers (Cloudflare) treat a
reused context — accumulating cookies and request history — as a suspicious
returning client and start challenging it, whereas a new context per request
looks like a new visitor. So we reuse the expensive browser process but keep the
cheap per-request isolation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


class GenericPlaywrightPool:
    """A fixed set of reused browser processes; a fresh context per fetch.

    Not safe to share across event loops; create one per batch run.
    """

    def __init__(self, *, size: int, user_agent: str, locale: str = "bg-BG") -> None:
        self._size = max(1, int(size))
        self._user_agent = user_agent
        self._locale = locale
        self._playwright: Any = None
        self._browsers: list[Any] = []
        self._next = 0
        self._start_lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._browsers) or self._size

    async def __aenter__(self) -> GenericPlaywrightPool:
        # Lazy: browsers are launched on first use so a batch that never needs
        # Playwright (HTTP suffices) pays nothing.
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._browsers:
            return
        async with self._start_lock:
            if self._browsers:
                return
            await self._start_locked()

    async def _start_locked(self) -> None:
        self._playwright = await async_playwright().start()
        for _ in range(self._size):
            browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._browsers.append(browser)
        logger.info("generic playwright pool started with %d browsers", len(self._browsers))

    async def _route(self, route: Any) -> None:
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    async def new_page(self) -> Any:
        """Create a fresh context on the next browser and open a page in it.

        The caller must close ``page.context`` when done (closing the context
        also closes the page), which is what the generic scraper does.
        """
        if not self._browsers:
            await self.start()
        browser = self._browsers[self._next % len(self._browsers)]
        self._next += 1
        context = await browser.new_context(
            locale=self._locale,
            user_agent=self._user_agent,
            viewport={"width": 1365, "height": 900},
        )
        await context.route("**/*", self._route)
        return await context.new_page()

    async def close(self) -> None:
        for browser in self._browsers:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        self._browsers = []
        self._playwright = None
