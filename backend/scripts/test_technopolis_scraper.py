#!/usr/bin/env python3
"""Run Technopolis PDP scraper once and print JSON (local dev helper).

Usage (from repository root):
    python backend/scripts/test_technopolis_scraper.py "https://www.technopolis.bg/bg/..."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.scrapers.sites.technopolis import TechnopolisScraper  # noqa: E402


def _json_default(obj: object) -> object:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError


def _result_to_dict(r) -> dict:
    return {
        "title": r.title,
        "price": str(r.price) if r.price is not None else None,
        "old_price": str(r.old_price) if r.old_price is not None else None,
        "promo_price": str(r.promo_price) if r.promo_price is not None else None,
        "currency": r.currency,
        "availability": r.availability,
        "image_url": r.image_url,
        "raw_data": r.raw_data,
    }


async def _main(url: str) -> None:
    scraper = TechnopolisScraper(url)
    result = await scraper.run()
    print(json.dumps(_result_to_dict(result), ensure_ascii=False, indent=2, default=_json_default))


def main() -> None:
    p = argparse.ArgumentParser(description="Test Technopolis product page scraper")
    p.add_argument("url", help="Full product detail URL on technopolis.bg")
    args = p.parse_args()
    asyncio.run(_main(args.url))


if __name__ == "__main__":
    main()
