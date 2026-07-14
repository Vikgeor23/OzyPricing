"""Demo / placeholder scraper returning synthetic listing data."""

from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any

from app.scrapers.base import BaseScraper, ScrapeResult


class DemoScraper(BaseScraper):
    """No network I/O — used until real per-site parsers exist."""

    async def fetch(self) -> str:
        return json.dumps({"source": "demo", "url": self.listing_url})

    def parse(self, raw: str) -> ScrapeResult:
        payload: dict[str, Any] = json.loads(raw)
        now = datetime.now(timezone.utc)
        payload["scraper_status"] = "success"
        payload["source"] = payload.get("source", "demo")
        return ScrapeResult(
            title="Demo competitor listing",
            price=Decimal("99.9900"),
            old_price=Decimal("129.9900"),
            promo_price=Decimal("89.9900"),
            currency="BGN",
            availability="in_stock",
            captured_at=now,
            image_url=None,
            raw_data=payload,
        )
