"""Generic product-page scraper for unknown ecommerce domains."""

from __future__ import annotations

import json
import logging
import re
import time
import html as html_lib
import asyncio
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.config import get_settings
from app.scrapers.base import BaseScraper, ScrapeResult
from app.utils.url_utils import normalize_url

logger = logging.getLogger(__name__)

# lxml parses large product pages ~10x faster than html.parser, which matters
# because parsing runs serialized on the event-loop thread during batches.
try:
    import lxml  # noqa: F401

    _SOUP_PARSER = "lxml"
except ImportError:  # pragma: no cover
    _SOUP_PARSER = "html.parser"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_BLOCKED_HTML_MARKERS = (
    "access denied",
    "captcha",
    "cf-browser-verification",
    "cloudflare",
    "please enable javascript",
)

# Notino renders its "product no longer sold" page with HTTP 200, so without
# this marker a removed product looks like a parse failure and is retried
# forever instead of being counted toward the not-found dead streak.
_NOTINO_REMOVED_MARKERS = (
    "нищо не се е увредило",
    "продуктът вече не се предлага",
)

# Cloudflare interstitial markers (narrower than _BLOCKED_HTML_MARKERS: these
# only appear on the challenge page itself, never on a real product page).
_CHALLENGE_PAGE_MARKERS = (
    "just a moment",
    "един момент",
    "cf-browser-verification",
    "cf-challenge",
    "challenge-platform",
    "checking your browser",
)

# Shared keep-alive HTTP client per event loop: batch scrapes issue tens of
# thousands of requests, and a per-request client would pay a fresh TCP + TLS
# handshake for every product.
_shared_http_client: httpx.AsyncClient | None = None
_shared_http_loop: asyncio.AbstractEventLoop | None = None


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client, _shared_http_loop
    loop = asyncio.get_running_loop()
    if _shared_http_client is None or _shared_http_loop is not loop:
        settings = get_settings()
        _shared_http_client = httpx.AsyncClient(
            timeout=settings.scrape_http_timeout_sec,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8"},
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        _shared_http_loop = loop
    return _shared_http_client

TITLE_SELECTORS = (
    'meta[property="og:title"]',
    'meta[name="twitter:title"]',
    '[itemprop="name"]',
    "h1",
    ".product-title",
    ".product__title",
    '[class*="product"][class*="title"]',
    "title",
)

PRICE_SELECTORS = (
    "#ProductPrice",
    ".single-product-price",
    ".product-detail-price",
    ".product-info .final-price",
    '[itemprop="price"]',
    'meta[property="product:price:amount"]',
    'meta[property="og:price:amount"]',
    "[data-price]",
    "[data-product-price]",
    ".current-price",
    ".sale-price",
    ".product-price",
    ".price-current",
    ".price",
    '[class*="price"]',
)

OLD_PRICE_SELECTORS = (
    "s",
    "del",
    "strike",
    ".old-price",
    ".was-price",
    ".regular-price",
    ".list-price",
    '[class*="old"][class*="price"]',
    '[class*="regular"][class*="price"]',
)

IMAGE_SELECTORS = (
    'meta[property="og:image"]',
    'meta[name="twitter:image"]',
    'link[rel="image_src"]',
    '[itemprop="image"]',
    "img.product-image",
    ".product-gallery img",
    "picture img",
)

DESCRIPTION_SELECTORS = (
    'meta[name="description"]',
    'meta[property="og:description"]',
    '[itemprop="description"]',
    ".product-description",
    ".description",
    "#description",
    '[class*="description"]',
)

ATTRIBUTE_LABELS = (
    "категория",
    "вид",
    "марка",
    "серия",
    "тема",
    "brand",
    "category",
    "type",
    "series",
    "theme",
)

PRODUCT_CODE_RE = re.compile(
    r"(?:№|no\.?|код|code|sku|арт\.?\s*№?)\s*[:#]?\s*([a-z0-9][a-z0-9._/-]{2,})",
    re.I,
)
# Checked before the loose label pattern: an explicit "SKU: X" wins over
# incidental "код"/"code" matches such as coupon boxes ("с код k10 за -10%").
PRODUCT_CODE_STRICT_RE = re.compile(r"\bsku\s*[:#]?\s*([a-z0-9][a-z0-9._/-]{1,})", re.I)

# Universal identifier digging: labeled values in page text and embedded JSON.
EAN_LABEL_RE = re.compile(
    r"(?:\bEAN\b|\bGTIN\b|баркод|barcode|щрих[\s-]?код)\s*(?:код|номер|№)?\s*[:\-–]?\s*(\d[\d\s]{6,16}\d)",
    re.I,
)
EAN_JSON_RE = re.compile(
    r"[\"'](?:\w{0,12}_)?(?:ean|gtin1[234]|gtin|barcode)[\"']\s*[:=]\s*[\"']?(\d{8,14})\b",
    re.I,
)
MPN_LABEL_RE = re.compile(
    r"(?:част(?:ен)?\s*номер|производителски\s*(?:код|номер)|кат\.?\s*(?:№|номер)|"
    r"\bP/N\b|\bMPN\b|part\s*(?:no\.?|number)|manufacturer\s*(?:code|part)|модел|model)\s*[:\-–]\s*"
    r"([A-Za-z0-9][A-Za-z0-9.\-_/]{2,40})",
    re.I,
)
ITEMPROP_EAN_SELECTORS = (
    '[itemprop="gtin13"]',
    '[itemprop="gtin14"]',
    '[itemprop="gtin12"]',
    '[itemprop="gtin8"]',
    '[itemprop="gtin"]',
    'meta[property="product:ean"]',
    'meta[property="og:barcode"]',
)
ITEMPROP_MPN_SELECTORS = ('[itemprop="mpn"]', 'meta[property="product:mfr_part_no"]')


def _is_valid_gtin(code: str) -> bool:
    """GTIN-8/12/13/14 length + mod-10 checksum — rejects prices/phones."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    if len(set(code)) == 1:
        return False
    digits = [int(c) for c in code]
    checksum = digits[-1]
    payload = digits[:-1][::-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(payload))
    return (10 - total % 10) % 10 == checksum


def _node_value(node: Any) -> str | None:
    value = node.get("content") or node.get("value") or node.get_text(" ", strip=True)
    return str(value).strip() if value else None


def _dig_ean(soup: BeautifulSoup, html: str, body_text: str) -> str | None:
    for selector in ITEMPROP_EAN_SELECTORS:
        node = soup.select_one(selector)
        if node is not None:
            value = re.sub(r"\D", "", _node_value(node) or "")
            if _is_valid_gtin(value):
                return value
    for match in EAN_JSON_RE.finditer(html):
        if _is_valid_gtin(match.group(1)):
            return match.group(1)
    for match in EAN_LABEL_RE.finditer(body_text):
        candidate = re.sub(r"\s", "", match.group(1))
        if _is_valid_gtin(candidate):
            return candidate
    return None


# Containers whose prices belong to OTHER products (related/recommended
# grids, carousels) or to page chrome (header minicart, footer, menus) —
# a price candidate found inside one of these must not win over nothing.
_NOISE_CONTAINER_RE = re.compile(
    r"related|similar|recommend|upsell|cross-?sell|also-?bought|carousel|slider|swiper|"
    r"recently|viewed|bestseller|featured|mini-?cart|footer|breadcrumb|\bnav\b|menu|sidebar",
    re.I,
)

_GTAG_PRICE_RE = re.compile(r"[\"']price[\"']\s*:\s*[\"']?(\d+(?:[.,]\d{1,2})?)")
_GTAG_CURRENCY_RE = re.compile(r"[\"']currency[\"']\s*:\s*[\"']([A-Za-z]{3})")


def _currency_shown_for_amount(html: str, price: Decimal) -> str | None:
    """Find how the page itself displays this exact amount ("7,00 €" → EUR).

    Resolves the currency of an analytics price that carries no currency
    field, on pages where CSS parsing produced no currency signal.
    """
    quantized = f"{price:.2f}"
    for amount in dict.fromkeys((quantized.replace(".", ","), quantized, str(price))):
        for match in re.finditer(re.escape(amount) + r"\s*([^\s<,\d][^\s<]{0,3})", html):
            currency = _currency_from_text(f"1 {match.group(1)}")
            if currency:
                return currency
    return None


def _dig_gtag_product(html: str) -> dict[str, Any]:
    """Price/currency of the viewed product from analytics payloads
    (GA4 ``gtag('event','view_item',…)`` / dataLayer ecommerce detail).

    These events describe the product page itself, so on shops without
    JSON-LD they are more trustworthy than CSS class guessing, which can
    land on a related-products card.
    """
    for marker in ("view_item", '"detail"', "'detail'"):
        idx = html.find(marker)
        while idx != -1:
            window = html[idx : idx + 1500]
            match = _GTAG_PRICE_RE.search(window)
            if match:
                try:
                    price = Decimal(match.group(1).replace(",", "."))
                except ArithmeticError:
                    price = None
                if price is not None and price > 0:
                    currency_match = _GTAG_CURRENCY_RE.search(window)
                    return {
                        "price": price,
                        "currency": currency_match.group(1).upper() if currency_match else None,
                    }
            idx = html.find(marker, idx + 1)
    return {}


def _dig_manufacturer_code(soup: BeautifulSoup, body_text: str) -> str | None:
    for selector in ITEMPROP_MPN_SELECTORS:
        node = soup.select_one(selector)
        if node is not None:
            value = _node_value(node)
            if value and 2 < len(value) <= 40:
                return value
    match = MPN_LABEL_RE.search(body_text)
    if match:
        candidate = match.group(1).strip(".-")
        # A bare number is more likely a size/price fragment than a part number.
        if not candidate.isdigit() or len(candidate) >= 5:
            return candidate
    return None

AVAILABILITY_PATTERNS = (
    (re.compile(r"\b(in stock|available|наличност|в наличност)\b", re.I), "in_stock"),
    (re.compile(r"\b(out of stock|sold out|изчерпан|няма наличност|не е наличен)\b", re.I), "out_of_stock"),
    (re.compile(r"\b(pre[- ]?order|очакваме|по заявка)\b", re.I), "preorder"),
)

SIZE_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:ml|мл|g|гр|kg|кг|l|л|oz)\b", re.I)

CURRENCY_MAP = {
    "лв": "BGN",
    "bgn": "BGN",
    "€": "EUR",
    "eur": "EUR",
    "$": "USD",
    "usd": "USD",
    "£": "GBP",
    "gbp": "GBP",
}

PRICE_RE = re.compile(
    r"(?:(?P<prefix>лв\.?|bgn|€|eur|\$|usd|£|gbp)\s*)?"
    r"(?P<num>\d[\d\s.,]*(?:[.,]\d{1,2})?)"
    r"\s*(?P<suffix>лв\.?|bgn|€|eur|\$|usd|£|gbp)?",
    re.I,
)

DOUGLAS_GRAPHQL_PRODUCT_QUERY = """
query DouglasProduct($search: String, $sku: String) {
  bySku: products(filter: {sku: {eq: $sku}}, pageSize: 1) {
    total_count
    items {
      __typename
      sku
      name
      url_key
      canonical_url
      price_range {
        minimum_price {
          regular_price { value currency }
          final_price { value currency }
          discount { amount_off percent_off }
        }
      }
      ... on ConfigurableProduct {
        configurable_options {
          attribute_code
          label
          values { value_index label }
        }
        variants {
          attributes { code label value_index }
          product {
            sku
            name
            url_key
            canonical_url
            price_range {
              minimum_price {
                regular_price { value currency }
                final_price { value currency }
                discount { amount_off percent_off }
              }
            }
          }
        }
      }
    }
  }
  bySearch: products(search: $search, pageSize: 40, currentPage: 1) {
    total_count
    items {
      __typename
      sku
      name
      url_key
      canonical_url
      price_range {
        minimum_price {
          regular_price { value currency }
          final_price { value currency }
          discount { amount_off percent_off }
        }
      }
      ... on ConfigurableProduct {
        configurable_options {
          attribute_code
          label
          values { value_index label }
        }
        variants {
          attributes { code label value_index }
          product {
            sku
            name
            url_key
            canonical_url
            price_range {
              minimum_price {
                regular_price { value currency }
                final_price { value currency }
                discount { amount_off percent_off }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _is_blocked_response(status_code: int, html: str) -> bool:
    if status_code in (401, 403, 429):
        return True
    if status_code >= 500:
        return True
    low = html[:5000].lower()
    return any(marker in low for marker in _BLOCKED_HTML_MARKERS)


def _normalize_currency(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip().lower().replace(".", "")
    return CURRENCY_MAP.get(raw) or (raw.upper() if len(raw) == 3 else None)


def _currency_satisfies_preference(currency: str | None, preference: str | None) -> bool:
    if preference is None:
        return True
    if currency == preference:
        return True
    return {currency, preference} == {"BGN", "EUR"}


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    match = PRICE_RE.search(text)
    number = match.group("num") if match else text
    normalized = re.sub(r"\s+", "", number)
    if not re.match(r"^[\d.,]+$", normalized):
        return None

    comma = normalized.count(",")
    dot = normalized.count(".")
    try:
        if comma and dot:
            last_comma = normalized.rfind(",")
            last_dot = normalized.rfind(".")
            sep = "." if last_dot > last_comma else ","
            whole, frac = normalized.rsplit(sep, 1)
            whole = whole.replace(",", "").replace(".", "")
            return Decimal(f"{whole}.{frac}")
        if comma:
            whole, frac = normalized.rsplit(",", 1)
            if len(frac) <= 2:
                return Decimal(f"{whole.replace(',', '').replace('.', '')}.{frac}")
            if len(frac) == 3:
                # Exactly three digits after the last comma → thousands group.
                return Decimal(normalized.replace(",", ""))
            # Four-plus digits can't be a thousands group — it's a decimal
            # fraction with trailing zeros (API prices like "21,9300").
            return Decimal(f"{whole.replace(',', '').replace('.', '')}.{frac}")
        if dot:
            whole, frac = normalized.rsplit(".", 1)
            if len(frac) <= 2:
                return Decimal(f"{whole.replace('.', '')}.{frac}")
            if len(frac) == 3:
                return Decimal(normalized.replace(".", ""))
            return Decimal(f"{whole.replace('.', '')}.{frac}")
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


# --- Notino custom pricing -------------------------------------------------
# Notino product pages embed an Apollo state where every variant carries the
# current selling price plus the supplier-recommended price:
#   "price":{"__typename":"Price","value":42.4,"currency":"EUR",...},
#   "originalPrice":{"__typename":"OriginalPrice","value":54,...,"type":"Recommended"}
# The JSON-LD offers array carries per-variant availability. Business rule:
# regular price = supplier-recommended, promo price = lowest in-stock selling
# price when it undercuts the recommended one (so the effective/final price is
# the lowest price visualised on the page).

_NOTINO_PAIR_RE = re.compile(
    r'"price":\{"__typename":"Price","value":([\d.]+),"currency":"([A-Z]{3})"[^}]*\}'
    r',"originalPrice":(?:\{"__typename":"OriginalPrice","value":([\d.]+)[^}]*"type":"(\w+)"|null)',
)
_NOTINO_INSTOCK_OFFER_RE = re.compile(
    r'"availability":"https://schema\.org/InStock","price":"([\d.]+)"',
)
_NOTINO_VOUCHER_PRICE_RE = re.compile(
    r'"priceAfterDiscount":\{[^{}]*"value":([\d.]+)',
)


def is_notino_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "notino.bg" or host.endswith(".notino.bg") or "notino." in host


def is_emag_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "emag.bg" or host.endswith(".emag.bg") or "emag." in host


# eMAG seller block variants (order matters — combined form first):
#   "Предлаган и с доставка от: <a>VENDOR</a>"           → same party sells and ships
#   "Предлаган от : <a>VENDOR</a> ... Доставка от: eMAG"  → marketplace seller, eMAG fulfils
_EMAG_COMBINED_SELLER_RE = re.compile(
    r"Предлаган\s+и\s+с\s+доставка\s+от\s*:.{0,200}?(?:<a[^>]*>|<span[^>]*>)\s*([^<]+?)\s*<",
    re.S,
)
_EMAG_OFFERED_BY_RE = re.compile(
    r"Предлаган\s+от\s*:.{0,200}?(?:<a[^>]*>|<span[^>]*>)\s*([^<]+?)\s*<",
    re.S,
)
_EMAG_DELIVERED_BY_RE = re.compile(
    r"Доставка\s+от\s*:\s*(?:<span[^>]*>|<a[^>]*>)?\s*([^<]+?)\s*<",
    re.S,
)


def _emag_seller_payload(html: str) -> dict[str, str] | None:
    """Extract "Предлаган от" / "Доставка от" vendor names from an eMAG page."""
    m = _EMAG_COMBINED_SELLER_RE.search(html)
    if m:
        vendor = html_lib.unescape(m.group(1)).strip()
        return {"offered_by": vendor, "delivered_by": vendor} if vendor else None
    offered = _EMAG_OFFERED_BY_RE.search(html)
    delivered = _EMAG_DELIVERED_BY_RE.search(html)
    payload: dict[str, str] = {}
    if offered and html_lib.unescape(offered.group(1)).strip():
        payload["offered_by"] = html_lib.unescape(offered.group(1)).strip()
    if delivered and html_lib.unescape(delivered.group(1)).strip():
        payload["delivered_by"] = html_lib.unescape(delivered.group(1)).strip()
    return payload or None


def _notino_price_payload(html: str) -> dict[str, Any] | None:
    """Extract (regular, lowest current, currency) from a Notino product page."""
    pairs = [
        (
            Decimal(m.group(1)),
            m.group(2),
            Decimal(m.group(3)) if m.group(3) else None,
            m.group(4),
        )
        for m in _NOTINO_PAIR_RE.finditer(html)
    ]
    if not pairs:
        return None
    in_stock = {Decimal(m.group(1)) for m in _NOTINO_INSTOCK_OFFER_RE.finditer(html)}
    candidates = [p for p in pairs if p[0] in in_stock] or pairs
    current, currency, recommended, kind = min(candidates, key=lambda p: p[0])
    lowest = current
    for m in _NOTINO_VOUCHER_PRICE_RE.finditer(html):
        voucher = Decimal(m.group(1))
        if voucher < lowest:
            lowest = voucher
    return {
        "regular": recommended if recommended is not None and kind == "Recommended" and recommended > 0 else None,
        "lowest": lowest,
        "currency": currency,
    }


# --- Configurable-product variant expansion --------------------------------
# Notino (and similar "configurable" shops) render every size of a product as a
# swatch on one page. Each size is a distinct sellable SKU with its own p-id
# URL, EAN, shop code and price, all embedded in the page's Apollo state as
# ``CatalogVariant`` objects. We expand each variant into its own listing row so
# a size is not lost and its price/identity are not collapsed into a sibling's.

_NOTINO_VARIANT_ANCHOR = '"__typename":"CatalogVariant"'


def _brace_slice(text: str, anchor_pos: int) -> str | None:
    """Return the JSON object enclosing ``anchor_pos`` via brace matching."""
    start = text.rfind("{", 0, anchor_pos)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _variant_size(node: dict[str, Any]) -> str | None:
    params = node.get("parameters")
    if isinstance(params, dict) and params.get("amount") is not None:
        amount = params.get("amount")
        unit = str(params.get("unit") or "").strip()
        combined = _first_size_from_text(f"{amount} {unit}")
        if combined:
            return combined
    return _first_size_from_text(str(node.get("additionalInfo") or ""))


def _notino_variants(html: str, base_url: str) -> list[dict[str, Any]]:
    """Extract one descriptor per size variant from a Notino product page."""
    origin = normalize_url(base_url)
    variants: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for match in re.finditer(re.escape(_NOTINO_VARIANT_ANCHOR), html):
        blob = _brace_slice(html, match.start())
        if not blob:
            continue
        try:
            node = json.loads(blob)
        except json.JSONDecodeError:
            continue
        rel_url = node.get("url")
        if not isinstance(rel_url, str) or not rel_url.strip():
            continue
        abs_url = normalize_url(urljoin(origin, rel_url.strip()))
        if abs_url in seen_urls:
            continue
        seen_urls.add(abs_url)

        price_node = node.get("price") if isinstance(node.get("price"), dict) else {}
        original_node = node.get("originalPrice") if isinstance(node.get("originalPrice"), dict) else {}
        price = _parse_decimal(price_node.get("value"))
        regular = _parse_decimal(original_node.get("value"))
        if regular is not None and str(original_node.get("type") or "") != "Recommended":
            regular = None
        currency = _normalize_currency(price_node.get("currency") or original_node.get("currency"))

        ean_raw = re.sub(r"\D", "", str(node.get("eanCode") or ""))
        ean = ean_raw if _is_valid_gtin(ean_raw) else None

        variants.append(
            {
                "url": abs_url,
                "size": _variant_size(node),
                "price": price,
                "regular": regular if regular and regular > 0 else None,
                "currency": currency,
                "ean": ean,
                "manufacturer_code": _first_text(node.get("productCode")),
                "shop_code": _first_text(node.get("orderCode")),
                "title": _first_text(node.get("name")),
            },
        )
    return variants


# Site-matcher → variant-extractor. Sites without an entry expand to nothing, so
# their scrape behaviour is unchanged. Add configurable shops here.
VARIANT_EXPANDERS: list[tuple[Any, Any]] = [
    (is_notino_url, _notino_variants),
]


def extract_variants(html: str, url: str) -> list[dict[str, Any]]:
    for matches, extractor in VARIANT_EXPANDERS:
        if matches(url):
            return extractor(html, url)
    return []


_PID_RE = re.compile(r"/p-(\d+)/?", re.I)


def _url_pid(url: str) -> str | None:
    match = _PID_RE.search(url or "")
    return match.group(1) if match else None


def _select_self_variant(
    variants: list[dict[str, Any]],
    listing_url: str,
    current_price: Decimal | None,
) -> dict[str, Any] | None:
    """Pick the variant this URL represents: by p-id when the URL carries one,
    else the one matching the page's effective price, else the cheapest."""
    pid = _url_pid(listing_url)
    if pid is not None:
        for variant in variants:
            if _url_pid(variant.get("url", "")) == pid:
                return variant
    if current_price is not None:
        for variant in variants:
            if variant.get("price") == current_price:
                return variant
    priced = [v for v in variants if v.get("price") is not None]
    if priced:
        return min(priced, key=lambda v: v["price"])
    return variants[0] if variants else None


def _apply_variant_identity(raw_data: dict[str, Any], variant: dict[str, Any]) -> None:
    """Align the scraped row's identity fields with its own size variant."""
    ids = dict(raw_data.get("product_identifiers") or {})
    if variant.get("ean"):
        ids["ean"] = variant["ean"]
    if variant.get("manufacturer_code"):
        ids["manufacturer_code"] = variant["manufacturer_code"]
    if variant.get("shop_code"):
        ids["shop_code"] = variant["shop_code"]
        ids["sku"] = variant["shop_code"]
    raw_data["product_identifiers"] = ids
    if variant.get("size"):
        raw_data["specs_json"] = {"size": variant["size"]}
        raw_ident = dict(raw_data.get("raw_identifiers") or {})
        raw_ident["size"] = variant["size"]
        attributes = dict(raw_ident.get("attributes") or {})
        attributes["size"] = variant["size"]
        raw_ident["attributes"] = attributes
        raw_data["raw_identifiers"] = raw_ident


def _currency_from_text(text: str) -> str | None:
    match = PRICE_RE.search(text)
    if not match:
        return None
    return _normalize_currency(match.group("prefix") or match.group("suffix"))


def _price_candidates_from_text(text: str) -> list[tuple[Decimal, str | None]]:
    candidates: list[tuple[Decimal, str | None]] = []
    for match in PRICE_RE.finditer(text):
        price = _parse_decimal(match.group("num"))
        if price is None or price <= 0:
            continue
        currency = _normalize_currency(match.group("prefix") or match.group("suffix"))
        candidates.append((price, currency))
    return candidates


def _first_size_from_text(text: str) -> str | None:
    match = SIZE_RE.search(text)
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(0)).upper().replace("МЛ", "ML").replace("ГР", "G").replace("КГ", "KG")


def _iter_json_nodes(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        yield payload
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_json_nodes(item)
        for key in ("offers", "itemListElement", "mainEntity", "isVariantOf"):
            value = payload.get(key)
            if isinstance(value, (dict, list)):
                yield from _iter_json_nodes(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_json_nodes(item)


def _node_type_contains(node: dict[str, Any], expected: str) -> bool:
    raw = node.get("@type") or node.get("type") or ""
    values = raw if isinstance(raw, list) else [raw]
    return any(expected.lower() in str(value).lower() for value in values)


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            value = value.get("url") or value.get("name") or value.get("@id")
        text = str(value).strip() if value is not None else ""
        if text:
            return text[:1024]
    return None


def _is_douglas_url(url: str) -> bool:
    host = urlparse(normalize_url(url)).netloc.lower()
    return host.removeprefix("www.") == "douglas.bg"


def _douglas_slug_from_url(url: str) -> str:
    return urlparse(normalize_url(url)).path.strip("/").split("/")[-1]


def _douglas_search_term_from_slug(slug: str) -> str:
    term = re.sub(r"-(?:conf-)?\d+$", "", slug, flags=re.I)
    return " ".join(part for part in term.split("-") if part)


def _douglas_sku_from_slug(slug: str) -> str:
    conf_match = re.search(r"-conf-(\d+)$", slug, re.I)
    if conf_match:
        return f"conf-{conf_match.group(1)}"
    match = re.search(r"-(\d+)$", slug)
    if not match:
        return ""
    # Douglas simple SKUs usually keep a leading zero not present in the URL slug.
    return match.group(1).zfill(6)


def _douglas_item_url_keys(item: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for key in ("canonical_url", "url_key"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            keys.add(value.strip().strip("/"))
    return keys


def _douglas_variant_size(variant: dict[str, Any]) -> str | None:
    attrs = variant.get("attributes")
    if not isinstance(attrs, list):
        return None
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        label = _first_text(attr.get("label"))
        if label and SIZE_RE.search(label):
            return _first_size_from_text(label) or label
    return None


def _douglas_price_payload(product: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, Decimal | None, str]:
    minimum = (((product.get("price_range") or {}).get("minimum_price") or {}) if isinstance(product, dict) else {})
    regular = minimum.get("regular_price") if isinstance(minimum.get("regular_price"), dict) else {}
    final = minimum.get("final_price") if isinstance(minimum.get("final_price"), dict) else {}
    regular_price = _parse_decimal(regular.get("value"))
    final_price = _parse_decimal(final.get("value"))
    currency = _normalize_currency(final.get("currency") or regular.get("currency")) or "EUR"
    if final_price is not None and regular_price is not None and final_price < regular_price:
        return final_price, regular_price, final_price, currency
    return final_price or regular_price, None, None, currency


def _douglas_find_product_match(payload: dict[str, Any], *, slug: str, sku: str) -> tuple[dict[str, Any], str | None] | None:
    buckets = []
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        for key in ("bySku", "bySearch"):
            bucket = data.get(key)
            if isinstance(bucket, dict):
                buckets.append(bucket)

    for bucket in buckets:
        items = bucket.get("items") if isinstance(bucket.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            if slug in _douglas_item_url_keys(item) or (sku and item.get("sku") == sku):
                return item, None
            variants = item.get("variants")
            if not isinstance(variants, list):
                continue
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                product = variant.get("product")
                if not isinstance(product, dict):
                    continue
                if slug in _douglas_item_url_keys(product) or (sku and product.get("sku") == sku):
                    return product, _douglas_variant_size(variant)
    return None


# --- Parse offloading --------------------------------------------------------
# One small process pool per worker process (created lazily on first use,
# fork-inherited modules make workers cheap). If the environment forbids child
# processes, the pool is marked broken and parsing falls back inline forever.
_PARSE_POOL: ProcessPoolExecutor | None = None
_PARSE_POOL_BROKEN = False


def _get_parse_pool() -> ProcessPoolExecutor | None:
    global _PARSE_POOL, _PARSE_POOL_BROKEN
    if _PARSE_POOL_BROKEN:
        return None
    settings = get_settings()
    if not settings.scrape_parse_offload_enabled:
        return None
    if _PARSE_POOL is None:
        try:
            # Celery prefork children carry multiprocessing's daemon flag, and
            # daemonic processes may not spawn children. The parse workers are
            # plain CPU helpers whose lifecycle we manage via the executor, so
            # dropping the flag here is safe (warm shutdown reaps them).
            from multiprocessing import current_process

            current_process()._config.pop("daemon", None)  # type: ignore[attr-defined]
            _PARSE_POOL = ProcessPoolExecutor(max_workers=max(1, settings.scrape_parse_pool_size))
        except Exception as exc:  # noqa: BLE001
            logger.warning("parse_pool_unavailable error=%s", exc)
            _PARSE_POOL_BROKEN = True
            return None
    return _PARSE_POOL


def _parse_html_job(
    listing_url: str,
    preferred_currency: str | None,
    html: str,
    extra_raw: dict[str, Any],
    captured_at: datetime,
) -> ScrapeResult:
    scraper = GenericProductScraper(listing_url, preferred_currency=preferred_currency)
    return scraper._parse_html_to_result(html, extra_raw=extra_raw, captured_at=captured_at)


class GenericProductScraper(BaseScraper):
    """Best-effort scraper for standard product detail pages."""

    def __init__(
        self,
        listing_url: str,
        *,
        preferred_currency: str | None = None,
        playwright_pool: Any = None,
    ) -> None:
        super().__init__(listing_url)
        self.preferred_currency = _normalize_currency(preferred_currency)
        self._playwright_pool = playwright_pool

    async def fetch(self) -> str:
        html, _, _, _ = await self._fetch_http()
        return html or ""

    async def _parse_html_async(
        self,
        html: str,
        *,
        extra_raw: dict[str, Any],
        captured_at: datetime,
    ) -> ScrapeResult:
        """Parse in the process pool so CPU-heavy pages don't stall the event loop."""
        global _PARSE_POOL_BROKEN
        settings = get_settings()
        pool = _get_parse_pool() if len(html) >= settings.scrape_parse_offload_min_bytes else None
        if pool is None:
            return self._parse_html_to_result(html, extra_raw=extra_raw, captured_at=captured_at)
        try:
            return await asyncio.get_running_loop().run_in_executor(
                pool,
                _parse_html_job,
                self.listing_url,
                self.preferred_currency,
                html,
                extra_raw,
                captured_at,
            )
        except (BrokenProcessPool, OSError, RuntimeError, AssertionError) as exc:
            logger.warning("parse_pool_failed url=%s error=%s — falling back inline", self.listing_url, exc)
            _PARSE_POOL_BROKEN = True
            return self._parse_html_to_result(html, extra_raw=extra_raw, captured_at=captured_at)

    def parse(self, raw: str) -> ScrapeResult:
        return self._parse_html_to_result(
            raw,
            extra_raw={"scraper_name": "generic_product", "fetch_layer": "unknown"},
            captured_at=datetime.now(timezone.utc),
        )

    async def run(self) -> ScrapeResult:
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        normalized_url = normalize_url(self.listing_url)

        if _is_douglas_url(normalized_url):
            douglas_result = await self._run_douglas_graphql(started=started, t0=t0)
            if douglas_result is not None:
                return douglas_result

        http_t0 = time.perf_counter()
        html, status_code, http_error, http_attempts = await self._fetch_http()
        http_ms = int((time.perf_counter() - http_t0) * 1000)
        diagnostics: dict[str, Any] = {
            "scraper_name": "generic_product",
            "url": normalized_url,
            "fetch_layer": "http",
            "http_status": status_code,
            "http_duration_ms": http_ms,
            "http_attempts": http_attempts,
        }
        if http_error:
            diagnostics["http_fetch_error"] = http_error
        if status_code == 429:
            diagnostics["http_blocked"] = True
            diagnostics["scrape_error_code"] = "rate_limited"
            return self._failure(started, t0, "status_429_rate_limited", diagnostics)
        if status_code == 404:
            diagnostics["http_fetch_failed"] = "status_404"
            diagnostics["scrape_error_code"] = "product_not_found"
            return self._failure(started, t0, "status_404_not_found", diagnostics)

        if html and status_code and not _is_blocked_response(status_code, html):
            result = await self._parse_html_async(html, extra_raw=diagnostics, captured_at=started)
            if result.raw_data.get("listing_page_detected"):
                diagnostics["scrape_error_code"] = "product_not_found"
                return self._failure(started, t0, "listing_page_not_product", diagnostics)
            if (
                self._is_usable(result)
                and self._matches_preferred_currency(result)
                and not self._should_try_playwright_after_http(result)
            ):
                return self._with_status(result, "success", t0)
            diagnostics["http_parse_incomplete"] = True
            if self._is_usable(result) and not self._matches_preferred_currency(result):
                diagnostics["http_currency_mismatch"] = {
                    "preferred": self.preferred_currency,
                    "actual": result.currency,
                }
            if self._is_usable(result) and self._should_try_playwright_after_http(result):
                diagnostics["http_price_selector_too_broad"] = result.raw_data.get("selectors", {}).get("price")

        html, pw_diag, pw_error = await self._fetch_playwright()
        diagnostics.update(pw_diag)
        diagnostics["fetch_layer"] = "playwright"
        if pw_error:
            return self._failure(started, t0, pw_error, diagnostics)

        result = await self._parse_html_async(html, extra_raw=diagnostics, captured_at=started)
        if result.raw_data.get("listing_page_detected"):
            diagnostics["scrape_error_code"] = "product_not_found"
            return self._failure(started, t0, "listing_page_not_product", diagnostics)
        if self._is_usable(result):
            return self._with_status(result, "success", t0)
        # Anti-bot wall: block-status responses (or an uncleared challenge) on
        # the browser layer mean the whole site is refusing us, not that this
        # one product is broken — flag it so the batch can trip its breaker.
        pw_status = int(diagnostics.get("playwright_status") or 0)
        if pw_status in (401, 403, 429, 511) or diagnostics.get("blocked_challenge"):
            diagnostics["blocked_signal"] = True
            diagnostics["scrape_error_code"] = "rate_limited" if pw_status == 429 else "http_blocked"
            return self._failure(
                started,
                t0,
                f"blocked_anti_bot_status_{pw_status or 'challenge'}",
                {**result.raw_data, **diagnostics},
            )
        if is_notino_url(self.listing_url) and any(m in html.lower() for m in _NOTINO_REMOVED_MARKERS):
            diagnostics["scrape_error_code"] = "product_not_found"
            return self._failure(started, t0, "product_removed_page", diagnostics)
        return self._failure(started, t0, "price_or_title_missing_after_generic_parse", result.raw_data)

    async def _run_douglas_graphql(self, *, started: datetime, t0: float) -> ScrapeResult | None:
        slug = _douglas_slug_from_url(self.listing_url)
        sku = _douglas_sku_from_slug(slug)
        search = _douglas_search_term_from_slug(slug)
        diagnostics: dict[str, Any] = {
            "scraper_name": "generic_product",
            "fetch_layer": "douglas_graphql",
            "url": normalize_url(self.listing_url),
            "douglas_slug": slug,
            "douglas_sku_guess": sku,
            "douglas_search": search,
        }
        gql_t0 = time.perf_counter()
        try:
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
                    home = await page.goto("https://douglas.bg/", wait_until="domcontentloaded", timeout=30_000)
                    diagnostics["douglas_home_status"] = home.status if home is not None else None
                    await page.wait_for_timeout(1_000)
                    payload = {
                        "query": DOUGLAS_GRAPHQL_PRODUCT_QUERY,
                        "variables": {"sku": sku, "search": search},
                    }
                    response = await page.evaluate(
                        """async (payload) => {
                            const response = await fetch('/graphql', {
                                method: 'POST',
                                headers: {'content-type': 'application/json'},
                                body: JSON.stringify(payload),
                            });
                            return {status: response.status, text: await response.text()};
                        }""",
                        payload,
                    )
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            diagnostics["douglas_graphql_error"] = str(exc)
            diagnostics["douglas_graphql_error_type"] = type(exc).__name__
            diagnostics["douglas_graphql_duration_ms"] = int((time.perf_counter() - gql_t0) * 1000)
            return None

        diagnostics["douglas_graphql_status"] = int(response.get("status") or 0)
        diagnostics["douglas_graphql_duration_ms"] = int((time.perf_counter() - gql_t0) * 1000)
        if diagnostics["douglas_graphql_status"] != 200:
            return None
        try:
            body = json.loads(str(response.get("text") or "{}"))
        except json.JSONDecodeError as exc:
            diagnostics["douglas_graphql_json_error"] = str(exc)
            return None
        match = _douglas_find_product_match(body, slug=slug, sku=sku)
        if match is None:
            diagnostics["douglas_graphql_match_missing"] = True
            return None

        product, size = match
        price, old_price, promo_price, currency = _douglas_price_payload(product)
        title = _first_text(product.get("name"))
        image_url = None
        raw_identifiers = {
            "product_code": _first_text(product.get("sku")),
            "attributes": {**({"size": size} if size else {})},
            "size": size,
        }
        raw_data = {
            **diagnostics,
            "scrape_timestamp": started.isoformat(),
            "parse_mode": "douglas_graphql",
            "confidence": 0.95 if title and price is not None else 0.5,
            "raw_identifiers": raw_identifiers,
            "specs_json": {**({"size": size} if size else {})},
            "product_identifiers": {
                "sku": _first_text(product.get("sku")),
                "shop_code": _first_text(product.get("sku")),
                "brand": title.split(" ", 1)[0] if title else None,
            },
            "douglas_graphql_product": {
                "sku": product.get("sku"),
                "url_key": product.get("url_key"),
                "canonical_url": product.get("canonical_url"),
                "size": size,
            },
        }
        result = ScrapeResult(
            title=title,
            price=price,
            old_price=old_price,
            promo_price=promo_price,
            currency=currency,
            availability="in_stock" if price is not None else None,
            captured_at=started,
            image_url=image_url,
            raw_data=raw_data,
        )
        if self._is_usable(result):
            return self._with_status(result, "success", t0)
        return None

    async def _fetch_http(self) -> tuple[str | None, int, str | None, int]:
        try:
            client = _get_shared_http_client()
            last_response: httpx.Response | None = None
            for attempt in range(1, 4):
                response = await client.get(normalize_url(self.listing_url))
                last_response = response
                if response.status_code != 429:
                    return response.text, response.status_code, None, attempt
                retry_after = response.headers.get("retry-after")
                try:
                    delay = min(60.0, max(10.0, float(retry_after))) if retry_after else 10.0 * attempt
                except ValueError:
                    delay = 10.0 * attempt
                await asyncio.sleep(delay)
            if last_response is not None:
                return last_response.text, last_response.status_code, None, 3
            return None, 0, "no_response", 0
        except Exception as exc:  # noqa: BLE001
            return None, 0, str(exc), 0

    async def _load_page_html(self, page: Any, diagnostics: dict[str, Any]) -> str:
        settings = get_settings()
        response = await page.goto(
            normalize_url(self.listing_url),
            wait_until="domcontentloaded",
            timeout=settings.scrape_navigation_timeout_ms,
        )
        if response is not None:
            diagnostics["playwright_status"] = response.status
        head = (await page.content())[:4000].lower()
        if any(m in head for m in _CHALLENGE_PAGE_MARKERS):
            # Cloudflare interstitial: the page replaces itself with the real
            # product page once the challenge clears — give it time instead of
            # parsing the interstitial and failing with a bogus "no price".
            deadline = time.monotonic() + max(4.0, float(settings.discovery_browser_challenge_wait_sec))
            cleared = False
            while time.monotonic() < deadline:
                await page.wait_for_timeout(2_000)
                head = (await page.content())[:4000].lower()
                if not any(m in head for m in _CHALLENGE_PAGE_MARKERS):
                    cleared = True
                    break
            diagnostics["challenge_cleared"] = cleared
            if not cleared:
                diagnostics["blocked_challenge"] = True
                diagnostics["scrape_error_code"] = "http_blocked"
        for selector in ("h1", '[itemprop="price"]', ".price", '[class*="price"]'):
            try:
                await page.wait_for_selector(
                    selector,
                    state="attached",
                    timeout=settings.scrape_price_selector_wait_ms,
                )
                diagnostics["selector_seen"] = selector
                break
            except PlaywrightError:
                continue
        return await page.content()

    async def _fetch_playwright(self) -> tuple[str, dict[str, Any], str | None]:
        t0 = time.perf_counter()
        diagnostics: dict[str, Any] = {
            "playwright_enabled": True,
            "user_agent": _USER_AGENT,
            "wait_strategy": "domcontentloaded",
        }
        # In a batch, reuse a pooled browser so we do not launch (and tear down)
        # a whole Chromium process for every single product.
        if self._playwright_pool is not None:
            diagnostics["playwright_pooled"] = True
            page = await self._playwright_pool.new_page()
            try:
                html = await self._load_page_html(page, diagnostics)
                diagnostics["playwright_duration_ms"] = int((time.perf_counter() - t0) * 1000)
                return html, diagnostics, None
            except Exception as exc:  # noqa: BLE001
                diagnostics["playwright_error_type"] = type(exc).__name__
                diagnostics["playwright_duration_ms"] = int((time.perf_counter() - t0) * 1000)
                return "", diagnostics, str(exc)
            finally:
                # The pool hands out a fresh context per page; closing the
                # context tears down the page and its cookies with it.
                try:
                    await page.context.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                try:
                    context = await browser.new_context(
                        locale="bg-BG",
                        user_agent=_USER_AGENT,
                        viewport={"width": 1365, "height": 900},
                    )
                    page = await context.new_page()

                    async def block_heavy_assets(route):  # type: ignore[no-untyped-def]
                        if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
                            await route.abort()
                        else:
                            await route.continue_()

                    await page.route("**/*", block_heavy_assets)
                    html = await self._load_page_html(page, diagnostics)
                    diagnostics["playwright_duration_ms"] = int((time.perf_counter() - t0) * 1000)
                    return html, diagnostics, None
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            diagnostics["playwright_error_type"] = type(exc).__name__
            diagnostics["playwright_duration_ms"] = int((time.perf_counter() - t0) * 1000)
            return "", diagnostics, str(exc)

    def _parse_html_to_result(
        self,
        html: str,
        *,
        extra_raw: dict[str, Any],
        captured_at: datetime,
    ) -> ScrapeResult:
        soup = BeautifulSoup(html, _SOUP_PARSER)

        structured = self._extract_structured_product(soup)
        title, title_selector = self._extract_title(soup)
        image_url, image_selector = self._extract_image(soup)
        product_code = structured.get("sku") or self._extract_product_code(soup)
        variant = self._extract_selected_variant(soup)

        # Fast path: trustworthy JSON-LD price means the expensive CSS-selector
        # hunt can be skipped. BGN and EUR are pegged (1 EUR = 1.95583 BGN), so
        # either satisfies a preference for the other.
        structured_satisfies = structured.get("price") is not None and _currency_satisfies_preference(
            _normalize_currency(structured.get("currency")),
            _normalize_currency(self.preferred_currency),
        )
        if structured_satisfies:
            selector_price, price_selector, selector_currency = None, "json_ld", None
        else:
            selector_price, price_selector, selector_currency = self._extract_first_price(
                soup,
                PRICE_SELECTORS,
                preferred_currency=self.preferred_currency,
            )
        old_price, old_price_selector, old_currency = self._extract_first_price(
            soup,
            OLD_PRICE_SELECTORS,
            preferred_currency=self.preferred_currency or selector_currency,
        )

        structured_price = structured.get("price")
        structured_currency = structured.get("currency")
        variant_promo_price = None
        if variant.get("price") is not None:
            price = variant.get("price")
            currency = variant.get("currency") or self.preferred_currency or "EUR"
            old_price = variant.get("old_price") or old_price
            variant_promo_price = variant.get("promo_price")
            price_selector = variant.get("selector") or price_selector
        elif (
            self.preferred_currency
            and selector_price is not None
            and selector_currency == self.preferred_currency
            and structured_currency
            and structured_currency != self.preferred_currency
        ):
            price = selector_price
            currency = selector_currency
        else:
            price = structured_price or selector_price
            currency = structured_currency or selector_currency or old_currency or self.preferred_currency or "BGN"
        # No structured/variant price means `price` came from CSS class
        # guessing, which can hit a related-products card. The analytics
        # view_item payload describes the viewed product itself — trust it.
        if structured_price is None and variant.get("price") is None:
            gtag = _dig_gtag_product(html)
            gtag_price = gtag.get("price")
            if gtag_price is not None and (price is None or price != gtag_price):
                price = gtag_price
                currency = (
                    gtag.get("currency")
                    or (_currency_shown_for_amount(html, gtag_price) if selector_currency is None else None)
                    or currency
                )
                price_selector = "gtag_view_item"
        title = structured.get("title") or title
        image_url = structured.get("image_url") or image_url

        body_text: str | None = None

        def get_body_text() -> str:
            nonlocal body_text
            if body_text is None:
                body_text = soup.get_text(" ", strip=True)
            return body_text

        availability = structured.get("availability") or self._extract_availability(get_body_text())

        # Dig for identifiers the structured data did not provide: microdata,
        # embedded JSON blobs, and labeled values in the page text.
        ean = structured.get("ean") or _dig_ean(soup, html, get_body_text())
        manufacturer_code = structured.get("manufacturer_code") or _dig_manufacturer_code(soup, get_body_text())
        # Sites like eMAG label the barcode as a product/part code; a code that
        # passes the GTIN checksum is an EAN regardless of what the label said.
        if ean is None and manufacturer_code and _is_valid_gtin(re.sub(r"\D", "", manufacturer_code)):
            ean = re.sub(r"\D", "", manufacturer_code)

        raw_data = {
            **extra_raw,
            "scrape_timestamp": captured_at.isoformat(),
            "parse_mode": "generic_html",
            **({"listing_page_detected": True} if structured.get("listing_page") else {}),
            "has_configurable_options": bool(soup.select_one(".swatch-attribute, [name^='super_attribute']")),
            "confidence": self._confidence(
                title=title,
                price=price,
                structured_price=structured.get("price"),
                price_selector=price_selector,
            ),
            "selectors": {
                "title": title_selector,
                "price": price_selector,
                "old_price": old_price_selector,
                "image": image_selector,
            },
            "price_parse": {
                "preferred_currency": self.preferred_currency,
                "selected_currency": currency,
                "selector_currency": selector_currency,
                "old_currency": old_currency,
                "structured_currency": structured_currency,
                "variant": variant or None,
            },
            "structured_data": structured.get("raw", {}),
            # Descriptions and free-form attributes are intentionally not
            # collected — only the variant size, which drives matching.
            "specs_json": {"size": variant["size"]} if variant.get("size") else None,
            "raw_identifiers": {
                "product_code": product_code,
                "attributes": {**({"size": variant["size"]} if variant.get("size") else {})},
                "size": variant.get("size"),
            },
            "product_identifiers": {
                "ean": ean,
                "manufacturer_code": manufacturer_code,
                "model": structured.get("model"),
                "brand": structured.get("brand"),
                "sku": product_code,
                "shop_code": product_code,
            },
        }

        promo_price = (
            variant_promo_price
            if variant_promo_price is not None
            else price if old_price is not None and price is not None and price < old_price else None
        )

        if is_notino_url(self.listing_url):
            notino = _notino_price_payload(html)
            if notino is not None:
                # Regular = supplier-recommended price; promo = lowest in-stock
                # price when below it, so the effective price is what the page
                # shows. Other sites are untouched.
                lowest = notino["lowest"]
                regular = notino["regular"]
                if regular is not None and lowest is not None and lowest < regular:
                    price = regular
                    promo_price = lowest
                    old_price = old_price or regular
                elif lowest is not None:
                    price = lowest
                    promo_price = None
                currency = notino["currency"] or currency
                raw_data["notino_price_rules"] = True

        if is_emag_url(self.listing_url):
            seller = _emag_seller_payload(html)
            if seller is not None:
                raw_data.update(seller)

        # Configurable-product expansion: split the page's size variants so each
        # size gets its own listing row. The variant matching *this* URL sets the
        # current row's price/identity (fixing the collapse where every size read
        # the cheapest sibling's price); the rest are carried for persist to
        # materialise as sibling rows.
        siblings: list[dict[str, Any]] | None = None
        all_variants = extract_variants(html, self.listing_url)
        if all_variants:
            self_variant = _select_self_variant(all_variants, self.listing_url, price)
            if self_variant is not None:
                v_price = self_variant.get("price")
                if v_price is not None:
                    regular = self_variant.get("regular")
                    if regular is not None and regular > v_price:
                        price = regular
                        promo_price = v_price
                        old_price = old_price or regular
                    else:
                        price = v_price
                        promo_price = None
                    currency = self_variant.get("currency") or currency
                _apply_variant_identity(raw_data, self_variant)
            siblings = [v for v in all_variants if v is not self_variant]

        return ScrapeResult(
            title=title,
            price=price,
            old_price=old_price,
            promo_price=promo_price,
            currency=currency,
            availability=availability,
            captured_at=captured_at,
            image_url=image_url,
            raw_data=raw_data,
            variants=siblings or None,
        )

    def _extract_structured_product(self, soup: BeautifulSoup) -> dict[str, Any]:
        listing_detected = False
        for script in soup.select('script[type="application/ld+json"]'):
            raw = script.string or script.get_text()
            if not raw or not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for node in _iter_json_nodes(payload):
                if not _node_type_contains(node, "Product"):
                    if any(
                        _node_type_contains(node, t)
                        for t in ("CollectionPage", "ItemList", "SearchResultsPage")
                    ):
                        listing_detected = True
                    continue
                offers = node.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if not isinstance(offers, dict):
                    offers = {}
                brand = node.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                elif isinstance(brand, list):
                    first = brand[0] if brand else None
                    brand = first.get("name") if isinstance(first, dict) else first
                # Sites routinely put the barcode in the wrong gtin* field
                # (e.g. a full EAN-13 under "gtin8"), so consider all of them
                # and keep the first that passes the GTIN checksum.
                ean = None
                for key in ("gtin13", "gtin14", "gtin12", "gtin8", "gtin"):
                    candidate = re.sub(r"\D", "", _first_text(node.get(key)) or "")
                    if _is_valid_gtin(candidate):
                        ean = candidate
                        break
                # Multi-variant pages (e.g. parfimo.bg) publish arrays for
                # price/priceCurrency — one entry per variant; the first is
                # the offer shown on the page.
                def _first_offer_value(value: Any) -> Any:
                    if isinstance(value, list):
                        return value[0] if value else None
                    return value

                offer_price = _first_offer_value(offers.get("price")) or _first_offer_value(
                    offers.get("lowPrice"),
                )
                offer_currency = _first_offer_value(offers.get("priceCurrency"))
                return {
                    "title": _first_text(node.get("name")),
                    "price": _parse_decimal(offer_price),
                    "currency": _normalize_currency(offer_currency),
                    "availability": self._normalize_availability(offers.get("availability")),
                    "image_url": _first_text(node.get("image")),
                    "ean": ean,
                    "manufacturer_code": _first_text(node.get("mpn")),
                    "sku": _first_text(node.get("sku")),
                    "model": _first_text(node.get("model")),
                    "brand": _first_text(brand),
                    "description": _first_text(node.get("description")),
                    "raw": {"source": "json_ld_product"},
                }
        # A category/search page (CollectionPage/ItemList) without any Product
        # node must not be scraped as a product — any price found would be a
        # random listing price.
        return {"listing_page": True} if listing_detected else {}

    def _extract_title(self, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        for selector in TITLE_SELECTORS:
            node = soup.select_one(selector)
            if node is None:
                continue
            value = node.get("content") or node.get_text(" ", strip=True)
            if value and len(value.strip()) > 1:
                return value.strip()[:512], selector
        return None, None

    def _extract_image(self, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        for selector in IMAGE_SELECTORS:
            node = soup.select_one(selector)
            if node is None:
                continue
            value = node.get("content") or node.get("href") or node.get("src")
            if value:
                return urljoin(normalize_url(self.listing_url), str(value)), selector
        return None, None

    def _extract_description(self, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        for selector in DESCRIPTION_SELECTORS:
            node = soup.select_one(selector)
            if node is None:
                continue
            value = node.get("content") or node.get_text(" ", strip=True)
            if value and len(value.strip()) > 10:
                decoded = html_lib.unescape(value.strip())
                if "<" in decoded and ">" in decoded:
                    decoded = BeautifulSoup(decoded, _SOUP_PARSER).get_text(" ", strip=True)
                decoded = re.sub(r"\s+", " ", decoded.strip())
                decoded = re.sub(r"\s+([.,;:!?])", r"\1", decoded)
                return decoded[:2000], selector
        return None, None

    def _extract_product_code(self, soup: BeautifulSoup) -> str | None:
        candidates: list[str] = []
        for selector in ("[itemprop='sku']", "[data-sku]", ".sku", ".product-code", '[class*="sku"]', '[class*="code"]'):
            for node in soup.select(selector)[:5]:
                text = " ".join(
                    str(x)
                    for x in (node.get("content"), node.get("data-sku"), node.get_text(" ", strip=True))
                    if x
                )
                if text:
                    candidates.append(text)
        body_sample = soup.get_text(" ", strip=True)[:5000]
        candidates.append(body_sample)
        for pattern in (PRODUCT_CODE_STRICT_RE, PRODUCT_CODE_RE):
            for text in candidates:
                match = pattern.search(text)
                if match:
                    return match.group(1).strip(" .,:;#")[:128]
        return None

    def _extract_attributes(self, soup: BeautifulSoup) -> dict[str, str]:
        out: dict[str, str] = {}

        def add(label: str, value: str) -> None:
            clean_label = re.sub(r"\s+", " ", label).strip(" :").lower()
            clean_value = re.sub(r"\s+", " ", value).strip(" :")
            clean_value = PRICE_RE.sub("", clean_value).strip(" :")
            if not clean_label or not clean_value or len(clean_value) > 160:
                return
            if clean_label in ATTRIBUTE_LABELS:
                out.setdefault(clean_label, clean_value)

        for row in soup.select("tr, li, .attribute, .attributes div, .product-attributes div")[:200]:
            text = row.get_text(" ", strip=True)
            if ":" in text:
                label, value = text.split(":", 1)
                add(label, value)

        body = soup.get_text(" ", strip=True)
        labels = "|".join(re.escape(x) for x in ATTRIBUTE_LABELS)
        pattern = re.compile(rf"\b({labels})\s*:\s*([^:]+?)(?=\s+(?:{labels})\s*:|$)", re.I)
        for match in pattern.finditer(body[:6000]):
            add(match.group(1), match.group(2))

        return out

    def _extract_selected_variant(self, soup: BeautifulSoup) -> dict[str, Any]:
        for root in soup.select(".swatch-attribute.size, .swatch-attribute[class*='size']"):
            selected = None
            for node in root.select(".swatch-attribute-options > div"):
                classes = " ".join(node.get("class") or [])
                if "border-[#9bdcd2]" in classes or "selected" in classes.lower() or "active" in classes.lower():
                    selected = node
                    break
            if selected is None:
                continue

            text = selected.get_text(" ", strip=True)
            size = _first_size_from_text(text)
            eur_prices = [p for p, currency in _price_candidates_from_text(text) if currency == "EUR"]
            if not size and not eur_prices:
                continue

            regular_price = eur_prices[0] if eur_prices else None
            promo_price = None
            for candidate in eur_prices[1:]:
                if regular_price is None or candidate < regular_price:
                    promo_price = candidate
                    break

            return {
                "size": size,
                "price": regular_price,
                "promo_price": promo_price,
                "old_price": None,
                "currency": "EUR" if regular_price is not None else None,
                "selector": ".swatch-attribute.size .selected",
            }

        label = soup.select_one(".swatch-attribute.size .product-option-label span[x-text]")
        text = label.get_text(" ", strip=True) if label else ""
        size = _first_size_from_text(text)
        return {"size": size} if size else {}

    def _price_node_context_ok(self, node: Any) -> bool:
        """False when the node clearly prices a DIFFERENT product: it sits in a
        related/carousel/chrome container, or is wrapped in a link that leads
        to another page (product cards wrap their price in the card link;
        a page's own price is never a link to elsewhere)."""
        own_path = urlparse(self.listing_url).path.rstrip("/")
        for ancestor in node.parents:
            name = getattr(ancestor, "name", None)
            if name is None or name == "[document]":
                break
            attrs = " ".join((" ".join(ancestor.get("class") or []), ancestor.get("id") or ""))
            if attrs.strip() and _NOISE_CONTAINER_RE.search(attrs):
                return False
            if name == "a":
                href_path = urlparse(str(ancestor.get("href") or "")).path.rstrip("/")
                if href_path and href_path != own_path:
                    return False
        return True

    def _extract_first_price(
        self,
        soup: BeautifulSoup,
        selectors: Iterable[str],
        *,
        preferred_currency: str | None = None,
    ) -> tuple[Decimal | None, str | None, str | None]:
        normalized_preference = _normalize_currency(preferred_currency)
        fallback: tuple[Decimal, str | None, str] | None = None
        fallback_with_currency: tuple[Decimal, str | None, str] | None = None
        for selector in selectors:
            for node in soup.select(selector)[:10]:
                # A price that provably belongs to another product (related
                # card, carousel, chrome) is worse than no price at all — no
                # price escalates to the browser-render fallback instead.
                if not self._price_node_context_ok(node):
                    continue
                text = " ".join(
                    str(x)
                    for x in (
                        node.get("content"),
                        node.get("data-price"),
                        node.get("data-product-price"),
                        node.get("data-value"),
                        node.get_text(" ", strip=True),
                    )
                    if x
                )
                for price, currency in _price_candidates_from_text(text):
                    if normalized_preference and currency == normalized_preference:
                        return price, selector, currency
                    if fallback_with_currency is None and currency is not None:
                        fallback_with_currency = (price, currency, selector)
                    if fallback is None:
                        fallback = (price, currency, selector)
                price = _parse_decimal(text)
                if price is not None:
                    currency = _currency_from_text(text)
                    if normalized_preference and currency == normalized_preference:
                        return price, selector, currency
                    if fallback_with_currency is None and currency is not None:
                        fallback_with_currency = (price, currency, selector)
                    if fallback is None:
                        fallback = (price, currency, selector)
        if fallback_with_currency is not None:
            price, currency, selector = fallback_with_currency
            return price, selector, currency
        if fallback is not None:
            price, currency, selector = fallback
            return price, selector, currency
        return None, None, None

    def _extract_availability(self, text: str) -> str | None:
        for pattern, normalized in AVAILABILITY_PATTERNS:
            if pattern.search(text):
                return normalized
        return None

    def _normalize_availability(self, value: Any) -> str | None:
        if value is None:
            return None
        compact = re.sub(r"[^a-zа-я0-9]+", "", str(value).lower())
        if "instock" in compact:
            return "in_stock"
        if "outofstock" in compact or "soldout" in compact:
            return "out_of_stock"
        if "preorder" in compact:
            return "preorder"
        return self._extract_availability(str(value).replace("/", " "))

    def _confidence(
        self,
        *,
        title: str | None,
        price: Decimal | None,
        structured_price: Decimal | None,
        price_selector: str | None,
    ) -> str:
        if title and structured_price is not None:
            return "high"
        if title and price is not None and price_selector:
            return "medium"
        if title or price is not None:
            return "low"
        return "none"

    def _is_usable(self, result: ScrapeResult) -> bool:
        return result.title is not None and result.price is not None

    def _matches_preferred_currency(self, result: ScrapeResult) -> bool:
        return _currency_satisfies_preference(
            _normalize_currency(result.currency),
            _normalize_currency(self.preferred_currency),
        )

    def _should_try_playwright_after_http(self, result: ScrapeResult) -> bool:
        return bool(result.raw_data.get("has_configurable_options"))

    def _with_status(self, result: ScrapeResult, status: str, t0: float) -> ScrapeResult:
        raw = dict(result.raw_data)
        raw["scraper_status"] = status
        raw["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        return ScrapeResult(
            title=result.title,
            price=result.price,
            old_price=result.old_price,
            promo_price=result.promo_price,
            currency=result.currency,
            availability=result.availability,
            captured_at=result.captured_at,
            image_url=result.image_url,
            raw_data=raw,
        )

    def _failure(
        self,
        captured_at: datetime,
        t0: float,
        error: str,
        raw_data: dict[str, Any],
    ) -> ScrapeResult:
        return ScrapeResult(
            title=None,
            price=None,
            old_price=None,
            promo_price=None,
            currency="EUR",
            availability=None,
            captured_at=captured_at,
            image_url=None,
            raw_data={
                **raw_data,
                "scraper_status": "failure",
                "error": error,
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            },
        )
