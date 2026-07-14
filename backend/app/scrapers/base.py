"""Abstract scraper contract + shared types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass
class ScrapeResult:
    """Normalized output from a site-specific scraper."""

    title: str | None
    price: Decimal | None
    old_price: Decimal | None
    promo_price: Decimal | None
    currency: str
    availability: str | None
    captured_at: datetime
    image_url: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)
    # Sibling size variants of a configurable product (Notino etc.), each a
    # descriptor dict with its own url/price/ean/size; expanded into their own
    # listing rows at persist time. None/empty for ordinary single-SKU pages.
    variants: list[dict[str, Any]] | None = None
    # Transient observability payload for backend logs/metrics. It is not part
    # of the parsed product data and persistence paths intentionally ignore it.
    traffic_metrics: dict[str, Any] | None = None


class BaseScraper(ABC):
    """Placeholder framework: fetch HTML / API, parse, return structured data."""

    def __init__(self, listing_url: str) -> None:
        self.listing_url = listing_url

    @abstractmethod
    async def fetch(self) -> str:
        """Retrieve raw payload (HTML, JSON string, etc.)."""

    @abstractmethod
    def parse(self, raw: str) -> ScrapeResult:
        """Parse raw payload into a structured result."""

    async def run(self) -> ScrapeResult:
        raw = await self.fetch()
        return self.parse(raw)
