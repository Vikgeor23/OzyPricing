#!/usr/bin/env python3
"""Read-only OCC path diagnostic (no scraper logic changes)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import get_settings
from app.scrapers.sites.technopolis_hybrid import scrape_technopolis_url
from app.scrapers.sites.technopolis_occ_api import (
    OCC_API_ORIGIN,
    OCC_PRODUCT_PATH,
    extract_product_code,
    fetch_occ_product,
    parse_occ_product_payload,
    scrape_technopolis_occ,
)
from app.services.scrape_fetch import scrape_layer_from_result
from datetime import datetime, timezone


async def probe_url(url: str) -> dict:
    settings = get_settings()
    code = extract_product_code(url)
    out: dict = {
        "url": url,
        "scrape_occ_enabled": settings.scrape_occ_enabled,
        "product_code": code,
    }
    if code:
        req_url = f"{OCC_API_ORIGIN}{OCC_PRODUCT_PATH}/{code}"
        out["occ_request_url"] = req_url
        status, payload, err = await fetch_occ_product(code)
        out["occ_response_status"] = status
        out["occ_error"] = err
        if payload:
            parsed = parse_occ_product_payload(
                payload,
                listing_url=url,
                captured_at=datetime.now(timezone.utc),
                product_code=code,
                occ_status=status,
            )
            out["occ_parse_success"] = parsed is not None
            out["occ_missing_price"] = not (isinstance(payload.get("price"), dict) and payload["price"].get("value") is not None)
            out["occ_missing_name"] = not bool((payload.get("name") or payload.get("title") or "").strip())
            if parsed:
                out["occ_price"] = str(parsed.price)
        occ_r, occ_d = await scrape_technopolis_occ(url)
        out["occ_wrapper_success"] = occ_r is not None
        out["occ_fallback_reason"] = occ_d.get("occ_fallback_reason")

    full = await scrape_technopolis_url(url, pool=None)
    out["hybrid_layer"] = scrape_layer_from_result(full)
    out["hybrid_status"] = full.raw_data.get("scraper_status")
    out["hybrid_occ_failed"] = full.raw_data.get("occ_api_failed")
    out["hybrid_duration_ms"] = full.raw_data.get("duration_ms")
    return out


async def main() -> None:
    urls = sys.argv[1:] or [
        "https://www.technopolis.bg/bg/TV-stojki/TV-Stojka--HAMA-108726/p/14251",
    ]
    for url in urls:
        row = await probe_url(url)
        print(json.dumps(row, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
