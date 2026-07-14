"""Extract product specifications and identifiers from Technopolis PDP HTML."""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

# Normalized spec keys
KEY_EAN = "ean"
KEY_MANUFACTURER_CODE = "manufacturer_code"
KEY_MODEL = "model"
KEY_BRAND = "brand"
KEY_STORAGE = "storage"
KEY_COLOR = "color"
KEY_MEMORY = "memory"

_EAN_LABELS = frozenset(
    {
        "barcode",
        "bar code",
        "баркод",
        "ean",
        "gtin",
        "european article number",
        "европейски номер",
    },
)
_MANUFACTURER_LABELS = frozenset(
    {
        "manufacturer code",
        "manufacturer",
        "mpn",
        "part number",
        "product code",
        "код",
        "продуктов код",
        "продуктов код",
        "артикул",
        "артикулен номер",
        "код на производител",
    },
)
_MODEL_LABELS = frozenset(
    {
        "model",
        "модел",
        "model number",
        "номер на модел",
    },
)
_BRAND_LABELS = frozenset(
    {
        "brand",
        "марка",
        "производител",
        "manufacturer brand",
    },
)
_STORAGE_LABELS = frozenset(
    {
        "storage",
        "capacity",
        "памет",
        "капацитет",
        "вътрешна памет",
        "internal storage",
    },
)
_COLOR_LABELS = frozenset(
    {
        "color",
        "colour",
        "цвят",
        "цветов",
    },
)
_MEMORY_LABELS = frozenset(
    {
        "memory",
        "ram",
        "оперативна памет",
        "работна памет",
    },
)


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _canonical_key(label: str) -> str | None:
    n = _norm_label(label)
    if not n:
        return None
    if any(k in n or n in k for k in _EAN_LABELS):
        return KEY_EAN
    if any(k in n for k in _MANUFACTURER_LABELS) and "brand" not in n and "марка" not in n:
        return KEY_MANUFACTURER_CODE
    if any(k in n or n in k for k in _MODEL_LABELS):
        return KEY_MODEL
    if any(k in n or n in k for k in _BRAND_LABELS):
        return KEY_BRAND
    if any(k in n for k in _STORAGE_LABELS):
        return KEY_STORAGE
    if any(k in n for k in _COLOR_LABELS):
        return KEY_COLOR
    if any(k in n for k in _MEMORY_LABELS):
        return KEY_MEMORY
    return None


def _clean_value(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def normalize_ean(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) in (8, 12, 13, 14):
        return digits
    if len(digits) >= 8:
        return digits
    return None


def _pairs_from_table(table: Tag) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for row in table.select("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            label = _clean_value(cells[0].get_text(" ", strip=True))
            val = _clean_value(cells[1].get_text(" ", strip=True))
            if label and val:
                pairs.append((label, val))
    return pairs


def _pairs_from_dl(dl: Tag) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    dts = dl.find_all("dt", recursive=False)
    for dt in dts:
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        label = _clean_value(dt.get_text(" ", strip=True))
        val = _clean_value(dd.get_text(" ", strip=True))
        if label and val:
            pairs.append((label, val))
    return pairs


def _pairs_from_spec_blocks(soup: BeautifulSoup) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    selectors = [
        "[class*='characteristic'] li",
        "[class*='specification'] li",
        "[class*='product-spec'] li",
        "[class*='attributes'] li",
        ".product-details li",
        "[data-testid*='spec']",
    ]
    for sel in selectors:
        for node in soup.select(sel):
            text = _clean_value(node.get_text(" ", strip=True))
            if ":" in text:
                left, _, right = text.partition(":")
                if left.strip() and right.strip():
                    pairs.append((left.strip(), right.strip()))
            elif node.select_one("span, strong, b"):
                label_el = node.select_one("span, strong, b")
                if label_el:
                    label = _clean_value(label_el.get_text(" ", strip=True))
                    val = _clean_value(node.get_text(" ", strip=True).replace(label, "", 1))
                    if label and val:
                        pairs.append((label, val))
    return pairs


def _pairs_from_json_ld(soup: BeautifulSoup) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") in ("Product", "ProductModel") or item.get("gtin") or item.get("sku"):
                if item.get("gtin"):
                    pairs.append(("gtin", str(item["gtin"])))
                if item.get("gtin13"):
                    pairs.append(("gtin13", str(item["gtin13"])))
                if item.get("sku"):
                    pairs.append(("sku", str(item["sku"])))
                if item.get("mpn"):
                    pairs.append(("mpn", str(item["mpn"])))
                brand = item.get("brand")
                if isinstance(brand, dict) and brand.get("name"):
                    pairs.append(("brand", str(brand["name"])))
                elif isinstance(brand, str):
                    pairs.append(("brand", brand))
    return pairs


def collect_spec_pairs(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Gather label/value pairs from tables, lists, and JSON-LD."""
    pairs: list[tuple[str, str]] = []
    for table in soup.select("table"):
        pairs.extend(_pairs_from_table(table))
    for dl in soup.select("dl"):
        pairs.extend(_pairs_from_dl(dl))
    pairs.extend(_pairs_from_spec_blocks(soup))
    pairs.extend(_pairs_from_json_ld(soup))

    # Visible text fallback: "Label: value" lines
    for line in soup.get_text("\n", strip=True).splitlines():
        line = line.strip()
        if ":" not in line or len(line) > 200:
            continue
        left, _, right = line.partition(":")
        if 2 <= len(left) <= 60 and 1 <= len(right) <= 120:
            pairs.append((left.strip(), right.strip()))

    return pairs


def extract_technopolis_product_specs(
    soup: BeautifulSoup,
    *,
    url_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Parse Technopolis PDP characteristics into normalized identifiers.

    Returns dict with keys: ean, manufacturer_code, model, brand, specs_json, raw_identifiers.
    """
    url_meta = url_meta or {}
    specs_json: dict[str, str] = {}
    raw_identifiers: dict[str, str] = {}

    for label, value in collect_spec_pairs(soup):
        raw_identifiers[label] = value
        canon = _canonical_key(label)
        if canon:
            specs_json.setdefault(canon, value)
        else:
            specs_json[_norm_label(label)] = value

    ean = normalize_ean(specs_json.get(KEY_EAN))
    if not ean:
        for label, val in raw_identifiers.items():
            if _canonical_key(label) == KEY_EAN:
                ean = normalize_ean(val)
                if ean:
                    break

    manufacturer_code = specs_json.get(KEY_MANUFACTURER_CODE)
    model = specs_json.get(KEY_MODEL)
    brand = specs_json.get(KEY_BRAND)

    product_code = url_meta.get("product_code")
    if product_code and not manufacturer_code:
        manufacturer_code = str(product_code)
    if product_code and not model:
        model = str(product_code)

    return {
        "ean": ean,
        "manufacturer_code": manufacturer_code,
        "model": model,
        "brand": brand,
        "specs_json": specs_json or None,
        "raw_identifiers": raw_identifiers or None,
    }
