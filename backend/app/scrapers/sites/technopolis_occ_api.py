"""Technopolis SAP Commerce OCC API — primary scrape path for PDP data."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

import httpx

from app.config import get_settings
from app.scrapers.base import ScrapeResult

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

OCC_API_ORIGIN = "https://api.technopolis.bg"
OCC_PRODUCT_PATH = "/videoluxcommercewebservices/v2/technopolis-bg/products"
OCC_REFERER = "https://www.technopolis.bg/"
MEDIA_ORIGIN = OCC_API_ORIGIN
OCC_TEST_PRODUCT_CODE = "14251"

_PRODUCT_CODE_RE = re.compile(r"/p/(\d+)(?:[/?#]|$)", re.I)


def extract_product_code(url: str) -> str | None:
    """Parse Technopolis PDP id from ``…/p/{productCode}``."""
    m = _PRODUCT_CODE_RE.search(url)
    return m.group(1) if m else None


def _occ_headers() -> dict[str, str]:
    return {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "bg-BG",
        "Referer": OCC_REFERER,
    }


def _occ_params() -> dict[str, str]:
    return {
        "fields": "FULL",
        "lang": "bg",
        "curr": "EUR",
    }


def _decimal_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def map_stock_availability(
    stock: dict[str, Any] | None,
    *,
    sold_out: bool | None = None,
    purchasable: bool | None = None,
) -> str | None:
    """Map OCC stock fields to a stable availability string."""
    if sold_out:
        return "out_of_stock"
    if purchasable is False:
        return "out_of_stock"

    status = ""
    if isinstance(stock, dict):
        status = str(stock.get("stockLevelStatus") or "").strip()

    if not status:
        return None

    low = status.lower()
    if low == "instock":
        return "in_stock"
    if low in ("outofstock", "out_of_stock"):
        return "out_of_stock"
    if low == "reserved":
        return "reserved"
    if low == "lowstock":
        return "low_stock"
    return low


def map_breadcrumb_categories(breadcrumb_datas: Any) -> list[str]:
    """Category path names from OCC ``breadcrumbDatas`` (exclude PDP leaf)."""
    if not isinstance(breadcrumb_datas, list):
        return []

    names: list[str] = []
    for item in breadcrumb_datas:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        url = str(item.get("url") or "")
        if not name:
            continue
        if "/p/" in url:
            continue
        names.append(name)
    return names


def map_classifications_to_specs_json(classifications: Any) -> dict[str, Any] | None:
    """Flatten OCC classifications into a specs dict keyed by feature name."""
    if not isinstance(classifications, list):
        return None

    specs: dict[str, Any] = {}
    for group in classifications:
        if not isinstance(group, dict):
            continue
        group_name = (group.get("name") or "").strip()
        features = group.get("features")
        if not isinstance(features, list):
            continue
        for feat in features:
            if not isinstance(feat, dict):
                continue
            feat_name = (feat.get("name") or "").strip()
            if not feat_name:
                continue
            values: list[str] = []
            for fv in feat.get("featureValues") or []:
                if isinstance(fv, dict) and fv.get("value") is not None:
                    values.append(str(fv["value"]).strip())
            if values:
                key = f"{group_name}: {feat_name}" if group_name else feat_name
                specs[key] = values[0] if len(values) == 1 else values

    return specs or None


def pick_image_url(images: Any) -> str | None:
    """Prefer product-detail image; fall back to first PRIMARY image."""
    if not isinstance(images, list):
        return None

    preferred_formats = ("videoluxProduct", "videoluxZoom", "videoluxGrid", "videoluxThumbnail")
    for fmt in preferred_formats:
        for img in images:
            if not isinstance(img, dict):
                continue
            if img.get("format") == fmt and img.get("url"):
                return _absolute_media_url(str(img["url"]))

    for img in images:
        if isinstance(img, dict) and img.get("imageType") == "PRIMARY" and img.get("url"):
            return _absolute_media_url(str(img["url"]))
    return None


def _absolute_media_url(path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return urljoin(MEDIA_ORIGIN, path)


def occ_product_request_url(product_code: str) -> str:
    return f"{OCC_API_ORIGIN}{OCC_PRODUCT_PATH}/{product_code}"


async def fetch_occ_product(
    product_code: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """
    Fetch OCC product JSON.

    Returns ``(status_code, payload_or_none, error_message)``.
    """
    settings = get_settings()
    url = occ_product_request_url(product_code)
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=settings.scrape_occ_timeout_sec,
            follow_redirects=True,
            headers=_occ_headers(),
        )

    try:
        resp = await client.get(url, params=_occ_params())
        if resp.status_code != 200:
            return resp.status_code, None, resp.text[:500] if resp.text else f"status_{resp.status_code}"
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return resp.status_code, None, f"invalid_json: {exc}"
        if not isinstance(payload, dict):
            return resp.status_code, None, "payload_not_object"
        return resp.status_code, payload, None
    except Exception as exc:  # noqa: BLE001
        return 0, None, str(exc)
    finally:
        if owns_client:
            await client.aclose()


def parse_occ_product_payload(
    payload: dict[str, Any],
    *,
    listing_url: str,
    captured_at: datetime,
    product_code: str,
    occ_status: int = 200,
) -> ScrapeResult | None:
    """
    Map OCC product JSON to ``ScrapeResult``.

    Returns ``None`` when required fields (name + price) are missing.
    """
    name = (payload.get("name") or payload.get("title") or "").strip() or None
    price_obj = payload.get("price") if isinstance(payload.get("price"), dict) else {}
    price = _decimal_price(price_obj.get("value"))
    currency = (price_obj.get("currencyIso") or "EUR").strip() or "EUR"

    if price is None or not name:
        return None

    stock = payload.get("stock") if isinstance(payload.get("stock"), dict) else {}
    stock_level = stock.get("stockLevel")
    availability = map_stock_availability(
        stock,
        sold_out=bool(payload.get("soldOut")),
        purchasable=payload.get("purchasable"),
    )

    brand = (payload.get("brand") or "").strip() or None
    ean = payload.get("ean")
    ean_str = str(ean).strip() if ean is not None and str(ean).strip() else None

    image_url = pick_image_url(payload.get("images"))
    breadcrumb_categories = map_breadcrumb_categories(payload.get("breadcrumbDatas"))
    specs_json = map_classifications_to_specs_json(payload.get("classifications"))

    product_identifiers: dict[str, Any] = {
        "technopolis_product_code": product_code,
        "brand": brand,
    }
    if ean_str:
        product_identifiers["ean"] = ean_str

    raw_data: dict[str, Any] = {
        "source": "occ_api",
        "scrape_layer": "occ_api",
        "fetch_layer": "occ_api",
        "occ_product_code": product_code,
        "occ_status": occ_status,
        "product_code": payload.get("code") or product_code,
        "stock_level": stock_level,
        "stock_level_status": stock.get("stockLevelStatus"),
        "breadcrumb_categories": breadcrumb_categories,
        "specs_json": specs_json,
        "product_identifiers": product_identifiers,
        "url": listing_url,
        "currency_detected": currency,
    }

    return ScrapeResult(
        title=name[:512],
        price=price,
        old_price=None,
        promo_price=None,
        currency=currency,
        availability=availability,
        captured_at=captured_at,
        image_url=image_url,
        raw_data=raw_data,
    )


def _occ_log(
    event: str,
    *,
    competitor_product_id: str | None,
    product_code: str | None,
    duration_ms: int | None = None,
    **fields: Any,
) -> None:
    parts = [
        event,
        f"competitor_product_id={competitor_product_id or '-'}",
        f"product_code={product_code or '-'}",
    ]
    if duration_ms is not None:
        parts.append(f"duration_ms={duration_ms}")
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    logger.info(" ".join(parts))


async def scrape_technopolis_occ(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    competitor_product_id: str | None = None,
) -> tuple[ScrapeResult | None, dict[str, Any]]:
    """
    Attempt OCC API scrape for a Technopolis PDP URL.

    Returns ``(result, diagnostics)``. ``result`` is set on success.
    """
    occ_t0 = time.perf_counter()
    diagnostics: dict[str, Any] = {
        "source": "occ_api",
        "occ_api_attempted": True,
    }
    product_code = extract_product_code(url)
    _occ_log(
        "occ_start",
        competitor_product_id=competitor_product_id,
        product_code=product_code,
    )
    if not product_code:
        diagnostics["occ_fallback_reason"] = "no_product_code"
        _occ_log(
            "occ_fallback_reason",
            competitor_product_id=competitor_product_id,
            product_code=None,
            duration_ms=int((time.perf_counter() - occ_t0) * 1000),
            reason="no_product_code",
        )
        return None, diagnostics

    diagnostics["occ_product_code"] = product_code
    request_url = occ_product_request_url(product_code)
    _occ_log(
        "occ_request_url",
        competitor_product_id=competitor_product_id,
        product_code=product_code,
        url=request_url,
    )
    status, payload, err = await fetch_occ_product(product_code, client=client)
    diagnostics["occ_status"] = status
    diagnostics["occ_request_url"] = request_url
    if err:
        diagnostics["occ_error"] = err

    _occ_log(
        "occ_response_status",
        competitor_product_id=competitor_product_id,
        product_code=product_code,
        status=status,
        duration_ms=int((time.perf_counter() - occ_t0) * 1000),
    )

    if status != 200 or not payload:
        reason = diagnostics.get("occ_fallback_reason") or f"status_{status}"
        diagnostics["occ_fallback_reason"] = reason
        _occ_log(
            "occ_fallback_reason",
            competitor_product_id=competitor_product_id,
            product_code=product_code,
            duration_ms=int((time.perf_counter() - occ_t0) * 1000),
            reason=reason,
        )
        return None, diagnostics

    captured_at = datetime.now(timezone.utc)
    name = (payload.get("name") or payload.get("title") or "").strip() or None
    price_obj = payload.get("price") if isinstance(payload.get("price"), dict) else {}
    has_price = price_obj.get("value") is not None
    result = parse_occ_product_payload(
        payload,
        listing_url=url,
        captured_at=captured_at,
        product_code=product_code,
        occ_status=status,
    )
    parse_ok = result is not None
    _occ_log(
        "occ_parse_success",
        competitor_product_id=competitor_product_id,
        product_code=product_code,
        success=parse_ok,
        occ_missing_price=not has_price,
        occ_missing_name=not bool(name),
    )
    if result is None:
        diagnostics["occ_fallback_reason"] = "missing_price_or_name"
        _occ_log(
            "occ_fallback_reason",
            competitor_product_id=competitor_product_id,
            product_code=product_code,
            duration_ms=int((time.perf_counter() - occ_t0) * 1000),
            reason="missing_price_or_name",
        )
        return None, diagnostics

    occ_ms = int((time.perf_counter() - occ_t0) * 1000)
    _occ_log(
        "occ_success",
        competitor_product_id=competitor_product_id,
        product_code=product_code,
        duration_ms=occ_ms,
        price=result.price,
    )
    return result, diagnostics
