#!/usr/bin/env python3
"""
Read-only audit: capture Technopolis PDP XHR/fetch traffic via Playwright.

Usage (from repo root):
  py backend/scripts/audit_technopolis_xhr.py
  py backend/scripts/audit_technopolis_xhr.py --urls url1 url2 ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from playwright.async_api import async_playwright

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Five PDPs across categories (stable public URLs; override via CLI).
DEFAULT_PRODUCT_URLS = [
    "https://www.technopolis.bg/bg/Smartfoni-i-Nosimi-Ustroistva/Smartfoni/Smartfon-Apple-iPhone-16-128GB-Cheren/p/000000123",
    "https://www.technopolis.bg/bg/Kompyutri-i-Tableti/Laptopi/Laptop-Lenovo-IdeaPad/p/000000456",
    "https://www.technopolis.bg/bg/Televizori-i-Audio/Televizori/Televizor-Samsung/p/000000789",
    "https://www.technopolis.bg/bg/Dom-i-Ofis/Prahosmukachki/Prahosmukachka-Dyson/p/000000321",
    "https://www.technopolis.bg/bg/Gaming/Gaming-Aksesoari/Gaming-Mishka-Logitech/p/000000654",
]

_PRICE_PATTERNS = (
    re.compile(r'"price"\s*:\s*["\d.]+', re.I),
    re.compile(r'"currentPrice"\s*:', re.I),
    re.compile(r'"salePrice"\s*:', re.I),
    re.compile(r'"amount"\s*:\s*[\d.]+', re.I),
    re.compile(r"\d+[.,]\d{2}\s*(лв|BGN|EUR)", re.I),
    re.compile(r"itemprop=[\"']price", re.I),
)
_AVAIL_PATTERNS = (
    re.compile(r'"availability"\s*:', re.I),
    re.compile(r'"inStock"\s*:', re.I),
    re.compile(r'"stock"\s*:', re.I),
    re.compile(r"в\s+наличност", re.I),
)
_EAN_PATTERNS = (
    re.compile(r'"gtin13?"\s*:', re.I),
    re.compile(r'"ean"\s*:', re.I),
    re.compile(r'"barcode"\s*:', re.I),
)
_CODE_PATTERNS = (
    re.compile(r'"sku"\s*:', re.I),
    re.compile(r'"productCode"\s*:', re.I),
    re.compile(r'"articleId"\s*:', re.I),
    re.compile(r'"/p/\d+"', re.I),
)
_VARIANT_PATTERNS = (
    re.compile(r'"variants"\s*:', re.I),
    re.compile(r'"variantId"\s*:', re.I),
    re.compile(r'"attributes"\s*:', re.I),
)


def _signals(text: str) -> dict[str, bool]:
    return {
        "price": any(p.search(text) for p in _PRICE_PATTERNS),
        "availability": any(p.search(text) for p in _AVAIL_PATTERNS),
        "ean": any(p.search(text) for p in _EAN_PATTERNS),
        "product_code": any(p.search(text) for p in _CODE_PATTERNS),
        "stock": bool(re.search(r'"stock(?:Quantity|Level|Count)?"\s*:', text, re.I)),
        "variants": any(p.search(text) for p in _VARIANT_PATTERNS),
    }


def _is_api_candidate(url: str, content_type: str, resource_type: str) -> bool:
    if resource_type in ("xhr", "fetch"):
        return True
    low = url.lower()
    if any(x in low for x in ("/api/", "/graphql", ".json", "/product", "/catalog", "/pdp")):
        return True
    if "json" in content_type.lower():
        return True
    return False


@dataclass
class CapturedRequest:
    url: str
    method: str
    status: int | None
    content_type: str
    resource_type: str
    response_sample: str
    signals: dict[str, bool] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    post_data: str | None = None


async def _discover_product_urls() -> list[str]:
    """Load product PDP URLs from Technopolis Product sitemap (one per category prefix)."""
    import httpx

    loc_re = re.compile(r"<loc>([^<]+)</loc>")
    async with httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "bg-BG,bg;q=0.9"},
    ) as client:
        r = await client.get("https://www.technopolis.bg/sitemapurl/Product-bg-EUR-0.xml")
        if r.status_code != 200 or "<loc>" not in r.text:
            return []
        urls = loc_re.findall(r.text)
        seen_prefix: set[str] = set()
        picked: list[str] = []
        for u in urls:
            if "/p/" not in u:
                continue
            slug = u.split("/bg/")[-1].split("/p/")[0]
            prefix = slug.split("/")[0] if slug else ""
            if prefix and prefix not in seen_prefix:
                seen_prefix.add(prefix)
                picked.append(u)
            if len(picked) >= 5:
                break
        return picked[:5] if picked else urls[:5]


async def _capture_page(url: str, *, nav_timeout_ms: int = 25_000) -> tuple[list[CapturedRequest], list[str]]:
    captured: list[CapturedRequest] = []
    notes: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            locale="bg-BG",
            user_agent=_USER_AGENT,
            viewport={"width": 1365, "height": 900},
        )
        page = await context.new_page()

        async def on_response(response: Any) -> None:
            try:
                req = response.request
                resource_type = req.resource_type
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
                if not _is_api_candidate(response.url, content_type, resource_type):
                    return
                body = ""
                try:
                    body = await response.text()
                except Exception as exc:
                    body = f"<unreadable: {exc}>"
                sample = body[:1000] if body else ""
                captured.append(
                    CapturedRequest(
                        url=response.url,
                        method=req.method,
                        status=response.status,
                        content_type=content_type,
                        resource_type=resource_type,
                        response_sample=sample,
                        signals=_signals(body[:8000] if body else ""),
                        request_headers={k.lower(): v for k, v in req.headers.items()},
                        response_headers={k.lower(): v for k, v in response.headers.items()},
                        post_data=req.post_data,
                    ),
                )
            except Exception as exc:
                notes.append(f"response_handler_error: {exc}")

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            await page.wait_for_timeout(4000)
            for sel in ('[itemprop="price"]', ".current-price", ".price"):
                try:
                    await page.wait_for_selector(sel, timeout=5000)
                    break
                except Exception:
                    continue
            await page.wait_for_timeout(2000)
        except Exception as exc:
            notes.append(f"navigation: {exc}")
        finally:
            await context.close()
            await browser.close()

    return captured, notes


def _serialize_run(url: str, captured: list[CapturedRequest], notes: list[str]) -> dict[str, Any]:
    interesting = [c for c in captured if any(c.signals.values()) or c.status == 200]
    by_url: dict[str, CapturedRequest] = {}
    for c in interesting:
        key = f"{c.method} {c.url}"
        prev = by_url.get(key)
        if prev is None or sum(prev.signals.values()) < sum(c.signals.values()):
            by_url[key] = c
    return {
        "product_url": url,
        "category_path": urlparse(url).path.split("/bg/")[-1].split("/p/")[0] if "/p/" in url else "",
        "notes": notes,
        "xhr_fetch_total": len(captured),
        "candidate_count": len(by_url),
        "candidates": [
            {
                "url": c.url,
                "method": c.method,
                "status": c.status,
                "content_type": c.content_type,
                "resource_type": c.resource_type,
                "signals": c.signals,
                "response_sample": c.response_sample,
                "request_headers": {
                    k: c.request_headers[k]
                    for k in ("user-agent", "accept", "accept-language", "referer", "cookie", "x-requested-with")
                    if k in c.request_headers
                },
                "set_cookie": c.response_headers.get("set-cookie", "")[:200],
                "post_data_preview": (c.post_data or "")[:300] or None,
            }
            for c in sorted(by_url.values(), key=lambda x: (-sum(x.signals.values()), x.url))
        ],
    }


async def _httpx_replay_probe(url: str, headers: dict[str, str]) -> dict[str, Any]:
    import httpx

    out: dict[str, Any] = {"url": url, "attempts": []}
    header_sets = [
        {"User-Agent": _USER_AGENT, "Accept-Language": "bg-BG,bg;q=0.9"},
        {**headers, "User-Agent": _USER_AGENT},
    ]
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for i, hdrs in enumerate(header_sets):
            try:
                r = await client.get(url, headers=hdrs)
                text = r.text[:2000]
                out["attempts"].append(
                    {
                        "variant": i,
                        "status": r.status_code,
                        "content_type": r.headers.get("content-type", ""),
                        "signals": _signals(text),
                        "sample": text[:500],
                    },
                )
            except Exception as exc:
                out["attempts"].append({"variant": i, "error": str(exc)})
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description="Technopolis XHR/fetch network audit")
    parser.add_argument("--urls", nargs="*", help="Override product PDP URLs")
    parser.add_argument("--json-out", type=Path, help="Write raw JSON results")
    args = parser.parse_args()

    urls = args.urls or await _discover_product_urls()
    if not urls:
        urls = DEFAULT_PRODUCT_URLS
        print("WARN: using placeholder URLs; pass --urls or fix discovery", file=sys.stderr)

    if len(urls) < 5:
        discovered = await _discover_product_urls()
        for u in discovered:
            if u not in urls:
                urls.append(u)
            if len(urls) >= 5:
                break

    urls = urls[:5]
    print(f"Auditing {len(urls)} product URLs...", file=sys.stderr)

    runs: list[dict[str, Any]] = []
    replay_probes: list[dict[str, Any]] = []

    for url in urls:
        print(f"  capture: {url}", file=sys.stderr)
        captured, notes = await _capture_page(url)
        run = _serialize_run(url, captured, notes)
        runs.append(run)

    # Aggregate top endpoints across runs
    endpoint_scores: dict[str, dict[str, Any]] = {}
    for run in runs:
        for c in run["candidates"]:
            if not any(c["signals"].values()):
                continue
            key = c["url"].split("?")[0]
            score = sum(c["signals"].values())
            cur = endpoint_scores.get(key)
            if cur is None or cur["score"] < score:
                endpoint_scores[key] = {
                    "url": key,
                    "method": c["method"],
                    "score": score,
                    "signals": c["signals"],
                    "sample": c["response_sample"],
                    "status": c["status"],
                    "content_type": c["content_type"],
                    "headers": c["request_headers"],
                }

    top = sorted(endpoint_scores.values(), key=lambda x: -x["score"])[:15]
    for ep in top[:5]:
        hdrs = ep.get("headers") or {}
        replay_probes.append(await _httpx_replay_probe(ep["url"], hdrs))

    payload = {"product_urls": urls, "runs": runs, "top_endpoints": top, "httpx_replay": replay_probes}
    json_path = args.json_out or (BACKEND_ROOT.parent / "docs" / "audits" / "technopolis_xhr_raw.json")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {json_path}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
