"""Bulk price fetch for douglas.bg via the storefront GraphQL API.

Instead of scraping each product page individually (one Chromium launch per
URL), a single browser session pages through the catalog GraphQL endpoint at
500 products per request. The 10 000-result Magento search cap is bypassed by
partitioning the catalog into price buckets and splitting any bucket that
still hits the cap.

Every configurable product is expanded into one entry per variant so each
size/разфасовка lands on its own row with its own SKU, alongside the parent
listing itself.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from playwright.async_api import async_playwright

from app.config import get_settings
from app.scrapers.base import ScrapeResult
from app.scrapers.sites.generic import (
    _USER_AGENT,
    _douglas_price_payload,
    _douglas_variant_size,
    _first_text,
)
from app.scrapers.sites.generic_discovery import _magento_product_url_from_item

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]

DOUGLAS_ORIGIN = "https://douglas.bg"

_PAGE_SIZE = 500
_SEARCH_CAP = 10_000
# Parallel in-browser GraphQL requests during the catalog fetch phase.
_FETCH_CONCURRENCY = 4
# Initial price partition edges (site currency). Buckets that still hit the
# search cap are split further at runtime.
_INITIAL_PRICE_EDGES: list[Decimal | None] = [
    Decimal("0"),
    Decimal("10"),
    Decimal("25"),
    Decimal("50"),
    Decimal("100"),
    Decimal("200"),
    None,
]

# Some shops (e.g. galen.bg) intermittently 500 on custom_attributesV2 for
# certain products, which nulls out the entire page in the response; those
# pages are refetched with the no-attrs variant (losing only barcode/ean).
_CUSTOM_ATTRS_BLOCK = """
      custom_attributesV2(filters: {}) {
        items {
          code
          ... on AttributeValue { value }
          ... on AttributeSelectedOptions { selected_options { label } }
        }
      }"""

_BULK_QUERY_TEMPLATE = """
query DouglasBulkProducts($pageSize: Int!, $currentPage: Int!, $from: String, $to: String) {
  products(filter: {price: {from: $from, to: $to}}, pageSize: $pageSize, currentPage: $currentPage) {
    total_count
    page_info {
      current_page
      total_pages
    }
    items {
      sku
      name
      url_key
      canonical_url
      stock_status
      image { url }
      price_range {
        minimum_price {
          regular_price { value currency }
          final_price { value currency }
        }
      }%(custom_attrs)s
      ... on ConfigurableProduct {
        variants {
          attributes { code label value_index }
          product {
            sku
            name
            url_key
            canonical_url
            stock_status
            image { url }
            price_range {
              minimum_price {
                regular_price { value currency }
                final_price { value currency }
              }
            }
          }
        }
      }
    }
  }
}
"""

_BULK_QUERY = _BULK_QUERY_TEMPLATE % {"custom_attrs": _CUSTOM_ATTRS_BLOCK}
_BULK_QUERY_NO_ATTRS = _BULK_QUERY_TEMPLATE % {"custom_attrs": ""}

# Identifier attribute names vary per Magento shop. The first non-empty match
# in each group is used; everything stays available raw in raw_identifiers.
# PRIMARY: the supplier/article number shown as the listing "Code" (the shop's
# own SKU then moves to shop_code). MFR: manufacturer part number / model.
# EXTRA: any additional code worth keeping as its own column.
_PRIMARY_CODE_ATTRS = (
    "nomenclature_number",
    "nomenclature",
    "article_number",
    "articul",
    "artikul",
    "item_number",
    "item_code",
    "catalog_number",
    "product_code",
)
_MFR_CODE_ATTRS = (
    "manufacturer_code",
    "manufacturer_sku",
    "manufacturer_number",
    "mfr_code",
    "mpn",
    "model",
    "model_number",
)
_EXTRA_CODE_ATTRS = (
    "supplier_code",
    "supplier_sku",
    "vendor_code",
    "vendor_sku",
    "reference",
    "isbn",
)


def _identifier_codes(attrs: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(primary_code, mfr_code, extra_code) resolved from shop-specific attribute names."""

    def first(names: tuple[str, ...], taken: tuple[str | None, ...] = ()) -> str | None:
        for name in names:
            value = _first_text(attrs.get(name))
            if value and value not in taken:
                return value
        return None

    primary = first(_PRIMARY_CODE_ATTRS)
    mfr = first(_MFR_CODE_ATTRS, taken=(primary,))
    extra = first(_EXTRA_CODE_ATTRS, taken=(primary, mfr))
    return primary, mfr, extra


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_DESCRIPTION_MAX_LEN = 2000
# Custom attribute codes copied into each row's attributes dict (promo_list_*
# flags mark eligibility for the current coupon campaign).
_PROMO_FLAG_PREFIX = "promo_list_"


def _strip_html(value: str) -> str:
    text = html_lib.unescape(_HTML_TAG_RE.sub(" ", value))
    return " ".join(text.split())


def _attrs_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten custom_attributesV2 into {code: value-or-label}."""
    flat: dict[str, Any] = {}
    container = item.get("custom_attributesV2")
    rows = container.get("items") if isinstance(container, dict) else None
    if not isinstance(rows, list):
        return flat
    for row in rows:
        if not isinstance(row, dict) or not row.get("code"):
            continue
        options = row.get("selected_options")
        if isinstance(options, list) and options:
            labels = [o.get("label") for o in options if isinstance(o, dict) and o.get("label")]
            flat[row["code"]] = labels[0] if len(labels) == 1 else labels
        elif row.get("value") is not None:
            flat[row["code"]] = row["value"]
    return flat


def _availability_from_stock_status(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.lower()


def _image_url_from_item(item: dict[str, Any]) -> str | None:
    image = item.get("image")
    if isinstance(image, dict) and isinstance(image.get("url"), str):
        return image["url"] or None
    return None


@dataclass
class DouglasBulkEntry:
    """One price observation: a simple product, a configurable parent, or a variant."""

    url: str
    sku: str | None
    title: str | None
    price: Decimal | None
    old_price: Decimal | None
    promo_price: Decimal | None
    currency: str
    size: str | None
    parent_sku: str | None = None
    ean: str | None = None
    primary_code: str | None = None
    mfr_code: str | None = None
    extra_code: str | None = None
    image_url: str | None = None
    description: str | None = None
    availability: str | None = None
    attributes: dict[str, Any] | None = None
    promo_code: str | None = None

    def to_scrape_result(self, *, captured_at: datetime) -> ScrapeResult:
        attributes = {
            **(self.attributes or {}),
            **({"size": self.size} if self.size else {}),
            **({"promo_code": self.promo_code} if self.promo_code else {}),
        }
        raw_data = {
            "scraper_name": "generic_product",
            "fetch_layer": "douglas_graphql_bulk",
            "parse_mode": "douglas_graphql_bulk",
            "url": self.url,
            "scrape_timestamp": captured_at.isoformat(),
            "confidence": 0.95 if self.title and self.price is not None else 0.5,
            "raw_identifiers": {
                "product_code": self.sku,
                "attributes": attributes,
                "size": self.size,
                **({"parent_sku": self.parent_sku} if self.parent_sku else {}),
                **({"primary_code": self.primary_code} if self.primary_code else {}),
                **({"mfr_code": self.mfr_code} if self.mfr_code else {}),
                **({"extra_code": self.extra_code} if self.extra_code else {}),
            },
            "specs_json": {**({"size": self.size} if self.size else {})},
            "product_identifiers": {
                "sku": self.primary_code or self.sku,
                "shop_code": self.sku,
                "ean": self.ean,
                "manufacturer_code": self.mfr_code,
                "model": self.mfr_code,
                "extra_code": self.extra_code,
                "brand": self.title.split(" ", 1)[0] if self.title else None,
            },
        }
        if self.price is None:
            raw_data["scraper_status"] = "failure"
            raw_data["failure_reason"] = "douglas_bulk_price_missing"
            raw_data["scrape_error_code"] = "unknown"
        return ScrapeResult(
            title=self.title,
            price=self.price,
            old_price=self.old_price,
            promo_price=self.promo_price,
            currency=self.currency,
            availability=self.availability or ("in_stock" if self.price is not None else None),
            captured_at=captured_at,
            image_url=self.image_url,
            raw_data=raw_data,
        )


def _coupon_promo_price(
    price: Decimal | None,
    attrs: dict[str, Any],
    *,
    douglas_rules: bool,
) -> tuple[Decimal | None, str | None]:
    """Compute the campaign coupon price for flagged products (e.g. BLACK30).

    The active campaign (flag attribute, percent, code) is configured because
    the site exposes only the eligibility flag, not the coupon price itself.
    """
    if not douglas_rules:
        return None, None
    settings = get_settings()
    flag = settings.douglas_promo_flag_attr
    percent = settings.douglas_promo_percent
    if not flag or not percent or price is None:
        return None, None
    if str(attrs.get(flag, "0")).strip() not in {"1", "true", "True"}:
        return None, None
    promo = (price * (Decimal("100") - Decimal(str(percent))) / Decimal("100")).quantize(Decimal("0.01"))
    return promo, settings.douglas_promo_code or None


def _entries_from_item(item: Any, *, origin: str, douglas_rules: bool = True) -> list[DouglasBulkEntry]:
    if not isinstance(item, dict):
        return []
    entries: list[DouglasBulkEntry] = []
    parent_url = _magento_product_url_from_item(item, origin=origin)
    parent_sku = _first_text(item.get("sku"))
    attrs = _attrs_from_item(item)

    # Descriptions are intentionally not collected.
    description = None
    # "barcode" on a configurable parent is a comma-joined list of the variant
    # barcodes (in variant order); a simple product has a single value. Other
    # Magento shops (e.g. hippoland.net) expose the same data as "ean".
    raw_barcode = _first_text(attrs.get("barcode")) or _first_text(attrs.get("ean")) or ""
    barcodes = [b.strip() for b in raw_barcode.split(",") if b.strip()]
    ean = barcodes[0] if len(barcodes) == 1 else None
    primary_code, mfr_code, extra_code = _identifier_codes(attrs)
    parent_size = _first_text(attrs.get("size")) if isinstance(attrs.get("size"), str) else (
        attrs["size"][0] if isinstance(attrs.get("size"), list) and attrs["size"] else None
    )
    promo_flags = {k: v for k, v in attrs.items() if k.startswith(_PROMO_FLAG_PREFIX)}
    shared_attributes: dict[str, Any] = {}

    if parent_url:
        price, old_price, promo_price, currency = _douglas_price_payload(item)
        if promo_price is None:
            promo_price, promo_code = _coupon_promo_price(price, attrs, douglas_rules=douglas_rules)
        else:
            promo_code = None
        previous_price = _first_text(attrs.get("previous_price"))
        if old_price is None and previous_price:
            try:
                prev = Decimal(previous_price)
                if price is not None and prev > price:
                    old_price = prev
            except ArithmeticError:
                pass
        entries.append(
            DouglasBulkEntry(
                url=parent_url,
                sku=parent_sku,
                title=_first_text(item.get("name")),
                price=price,
                old_price=old_price,
                promo_price=promo_price,
                currency=currency,
                size=parent_size,
                ean=ean,
                primary_code=primary_code,
                mfr_code=mfr_code,
                extra_code=extra_code,
                image_url=_image_url_from_item(item),
                description=description,
                availability=_availability_from_stock_status(item.get("stock_status")),
                attributes=shared_attributes,
                promo_code=promo_code,
            ),
        )
    variants = item.get("variants")
    if not isinstance(variants, list):
        return entries
    # Assign parent barcodes to variants positionally only when counts match.
    variant_eans = barcodes if len(barcodes) == len(variants) and len(barcodes) > 1 else []
    for index, variant in enumerate(variants):
        if not isinstance(variant, dict):
            continue
        product = variant.get("product")
        if not isinstance(product, dict):
            continue
        url = _magento_product_url_from_item(product, origin=origin)
        if not url:
            continue
        price, old_price, promo_price, currency = _douglas_price_payload(product)
        if promo_price is None:
            promo_price, promo_code = _coupon_promo_price(price, attrs, douglas_rules=douglas_rules)
        else:
            promo_code = None
        entries.append(
            DouglasBulkEntry(
                url=url,
                sku=_first_text(product.get("sku")),
                title=_first_text(product.get("name")) or _first_text(item.get("name")),
                price=price,
                old_price=old_price,
                promo_price=promo_price,
                currency=currency,
                size=_douglas_variant_size(variant),
                parent_sku=parent_sku,
                ean=variant_eans[index] if index < len(variant_eans) else (ean if len(variants) == 1 else None),
                # Parent-level codes describe the variant only when it is the sole one.
                primary_code=primary_code if len(variants) == 1 else None,
                mfr_code=mfr_code if len(variants) == 1 else None,
                extra_code=extra_code if len(variants) == 1 else None,
                image_url=_image_url_from_item(product) or _image_url_from_item(item),
                description=description,
                availability=_availability_from_stock_status(product.get("stock_status")),
                attributes=shared_attributes,
                promo_code=promo_code,
            ),
        )
    return entries


def _bucket_variables(frm: Decimal | None, to: Decimal | None) -> dict[str, Any]:
    return {
        "from": str(frm) if frm is not None else None,
        "to": str(to) if to is not None else None,
    }


_PROBE_QUERY = """
{ products(filter: {}, pageSize: 1, currentPage: 1) {
    total_count
    items { sku url_key price_range { minimum_price { final_price { value currency } } } }
} }
"""


def _probe_products_valid(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    products = (body.get("data") or {}).get("products")
    if not isinstance(products, dict):
        return False
    return bool(products.get("total_count")) and isinstance(products.get("items"), list)


async def detect_magento_bulk_transport(origin: str) -> str | None:
    """Return the fastest working bulk transport for a shop, or None.

    Tries a one-product catalog query against ``{origin}/graphql`` directly
    ("http"); when that looks bot-blocked (not merely absent), retries from a
    real browser page ("browser"). Returns None for non-Magento shops.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }
    blocked = False
    try:
        async with httpx.AsyncClient(timeout=12.0, headers=headers, follow_redirects=True) as client:
            resp = await client.post(f"{origin}/graphql", json={"query": _PROBE_QUERY})
            if resp.status_code == 200:
                try:
                    if _probe_products_valid(resp.json()):
                        return "http"
                except ValueError:
                    pass
            blocked = resp.status_code in {403, 429, 503}
    except Exception:  # noqa: BLE001
        blocked = True
    if not blocked:
        return None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context = await browser.new_context(locale="bg-BG", user_agent=_USER_AGENT)
                page = await context.new_page()
                await page.goto(origin, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(800)
                result = await page.evaluate(
                    """async (query) => {
                        const response = await fetch('/graphql', {
                            method: 'POST',
                            headers: {'content-type': 'application/json'},
                            body: JSON.stringify({query}),
                        });
                        return {status: response.status, text: await response.text()};
                    }""",
                    _PROBE_QUERY,
                )
                if int(result.get("status") or 0) == 200 and _probe_products_valid(
                    json.loads(str(result.get("text") or "{}")),
                ):
                    return "browser"
            finally:
                await browser.close()
    except Exception:  # noqa: BLE001
        logger.info("magento_bulk_browser_probe_failed origin=%s", origin)
    return None


async def fetch_magento_bulk_entries(
    origin: str = DOUGLAS_ORIGIN,
    *,
    transport: str = "browser",
    douglas_rules: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[DouglasBulkEntry], dict[str, Any]]:
    """Fetch price entries for a whole Magento catalog via its GraphQL API.

    ``transport="browser"`` runs the queries from a real page context (needed
    behind Cloudflare, e.g. douglas.bg); ``transport="http"`` posts directly
    to ``{origin}/graphql`` (works on open shops, e.g. hippoland.net).
    Returns deduplicated entries (unique by URL; parents and each variant
    separately) plus a diagnostics dict.
    """
    t0 = time.perf_counter()
    errors: list[str] = []
    pages_fetched = 0
    entries_by_url: dict[str, DouglasBulkEntry] = {}
    skus_seen: set[str] = set()
    catalog_total = 0
    pages_total = 0

    def emit(phase: str) -> None:
        if progress_callback is None:
            return
        payload = {
            "current_phase": phase,
            "pages_scanned": pages_fetched,
            "product_urls_found": len(entries_by_url),
            "catalog_total": catalog_total,
        }
        if pages_total:
            payload["pages_total"] = pages_total
            elapsed_min = max((time.perf_counter() - t0) / 60.0, 1e-6)
            payload["pages_per_minute"] = round(pages_fetched / elapsed_min, 1)
        progress_callback(payload)

    # RunQuery takes (variables, query) and returns (products, had_graphql_errors).
    RunQuery = Callable[[dict[str, Any], str], Any]
    no_attrs_retries = 0

    def handle_response(
        status: int,
        text: str,
        variables: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        if status != 200:
            errors.append(f"magento_bulk_status_{status}:{variables}")
            return None, False
        try:
            body = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            errors.append(f"magento_bulk_json_error:{exc}")
            return None, False
        had_graphql_errors = bool(body.get("errors"))
        if had_graphql_errors:
            errors.append(f"magento_bulk_graphql_errors:{json.dumps(body['errors'])[:200]}")
        products = (body.get("data") or {}).get("products")
        return (products if isinstance(products, dict) else None), had_graphql_errors

    async def process_catalog(run_query: RunQuery) -> None:
        nonlocal pages_fetched, no_attrs_retries, catalog_total, pages_total

        def collect_items(products: dict[str, Any]) -> int:
            items = products.get("items") if isinstance(products.get("items"), list) else []
            for item in items:
                parent_sku = item.get("sku") if isinstance(item, dict) else None
                if isinstance(parent_sku, str) and parent_sku in skus_seen:
                    continue
                if isinstance(parent_sku, str):
                    skus_seen.add(parent_sku)
                for entry in _entries_from_item(item, origin=origin, douglas_rules=douglas_rules):
                    entries_by_url.setdefault(entry.url, entry)
            return len(items)

        def items_intact(products: dict[str, Any] | None) -> bool:
            if products is None:
                return False
            items = products.get("items")
            return not isinstance(items, list) or all(isinstance(i, dict) for i in items)

        async def run_query_resilient(variables: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal no_attrs_retries
            products, had_graphql_errors = await run_query(variables, _BULK_QUERY)
            if not had_graphql_errors or items_intact(products):
                return products
            # A GraphQL error (typically custom_attributesV2 blowing up server
            # side) nulled part or all of the page; salvage what came back and
            # refetch without the attrs block.
            if products is not None:
                collect_items(products)
            no_attrs_retries += 1
            retry_products, _ = await run_query(variables, _BULK_QUERY_NO_ATTRS)
            return retry_products if retry_products is not None else products

        async def bucket_count(frm: Decimal | None, to: Decimal | None) -> int:
            products = await run_query_resilient({"pageSize": 1, "currentPage": 1, **_bucket_variables(frm, to)})
            if products is None:
                return 0
            return int(products.get("total_count") or 0)

        # Build leaf buckets, splitting any that hit the search cap.
        pending: list[tuple[Decimal | None, Decimal | None]] = [
            (_INITIAL_PRICE_EDGES[i], _INITIAL_PRICE_EDGES[i + 1])
            for i in range(len(_INITIAL_PRICE_EDGES) - 1)
        ]
        buckets: list[tuple[Decimal | None, Decimal | None]] = []
        while pending:
            frm, to = pending.pop(0)
            count = await bucket_count(frm, to)
            if count < _SEARCH_CAP:
                if count > 0:
                    buckets.append((frm, to))
                    catalog_total += count
                continue
            low = frm or Decimal("0")
            mid = (low + to) / 2 if to is not None else max(low * 2, low + Decimal("100"))
            if to is not None and (to - low) < Decimal("0.5"):
                errors.append(f"magento_bulk_bucket_unsplittable:{low}-{to}")
                buckets.append((frm, to))
                catalog_total += count
                continue
            pending.append((frm, mid))
            pending.append((mid, to))
        emit("douglas_bulk_buckets_ready")

        fetch_semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def fetch_page(frm: Decimal | None, to: Decimal | None, page_number: int) -> int:
            """Fetch one catalog page; returns the bucket's total_pages."""
            nonlocal pages_fetched
            async with fetch_semaphore:
                products = await run_query_resilient(
                    {
                        "pageSize": _PAGE_SIZE,
                        "currentPage": page_number,
                        **_bucket_variables(frm, to),
                    },
                )
            if products is None:
                return 0
            pages_fetched += 1
            collect_items(products)
            emit("douglas_bulk_fetching")
            page_info = products.get("page_info") if isinstance(products.get("page_info"), dict) else {}
            return int(page_info.get("total_pages") or 0)

        # First page of every bucket in parallel to learn page counts,
        # then all remaining pages in parallel.
        first_page_totals = await asyncio.gather(
            *(fetch_page(frm, to, 1) for frm, to in buckets),
        )
        pages_total = sum(max(1, t) for t in first_page_totals)
        emit("douglas_bulk_fetching")
        remaining = [
            fetch_page(frm, to, page_number)
            for (frm, to), total_pages in zip(buckets, first_page_totals)
            for page_number in range(2, total_pages + 1)
        ]
        if remaining:
            await asyncio.gather(*remaining)

    emit("douglas_bulk_start")
    try:
        if transport == "http":
            headers = {
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/json",
                "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
            }
            async with httpx.AsyncClient(timeout=60.0, headers=headers, follow_redirects=True) as client:

                async def run_query_http(
                    variables: dict[str, Any],
                    query: str,
                ) -> tuple[dict[str, Any] | None, bool]:
                    try:
                        resp = await client.post(
                            f"{origin}/graphql",
                            json={"query": query, "variables": variables},
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"magento_bulk_http_error:{type(exc).__name__}:{exc}")
                        return None, False
                    return handle_response(resp.status_code, resp.text, variables)

                await process_catalog(run_query_http)
        else:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    context = await browser.new_context(locale="bg-BG", user_agent=_USER_AGENT)

                    async def block_heavy_assets(route):  # type: ignore[no-untyped-def]
                        if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
                            await route.abort()
                        else:
                            await route.continue_()

                    await context.route("**/*", block_heavy_assets)
                    page = await context.new_page()
                    home = await page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
                    if home is None or home.status >= 400:
                        errors.append(f"magento_bulk_home_status_{home.status if home else 'none'}")
                    await page.wait_for_timeout(1_000)

                    async def run_query_browser(
                        variables: dict[str, Any],
                        query: str,
                    ) -> tuple[dict[str, Any] | None, bool]:
                        result = await page.evaluate(
                            """async (payload) => {
                                const response = await fetch('/graphql', {
                                    method: 'POST',
                                    headers: {'content-type': 'application/json'},
                                    body: JSON.stringify(payload),
                                });
                                return {status: response.status, text: await response.text()};
                            }""",
                            {"query": query, "variables": variables},
                        )
                        return handle_response(int(result.get("status") or 0), str(result.get("text") or ""), variables)

                    await process_catalog(run_query_browser)
                finally:
                    await browser.close()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"magento_bulk_error:{type(exc).__name__}:{exc}")
        logger.exception("magento_bulk_fetch_failed origin=%s", origin)

    entries = list(entries_by_url.values())
    diagnostics = {
        "source": "magento_graphql_bulk",
        "origin": origin,
        "transport": transport,
        "pages_fetched": pages_fetched,
        "entries_found": len(entries),
        "parent_skus_seen": len(skus_seen),
        "no_attrs_retries": no_attrs_retries,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "errors": errors[:50],
    }
    emit("douglas_bulk_done")
    return entries, diagnostics


async def fetch_douglas_bulk_entries(
    origin: str = DOUGLAS_ORIGIN,
    *,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[DouglasBulkEntry], dict[str, Any]]:
    """Backwards-compatible wrapper: douglas.bg bulk fetch via browser."""
    return await fetch_magento_bulk_entries(
        origin,
        transport="browser",
        douglas_rules=True,
        progress_callback=progress_callback,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
