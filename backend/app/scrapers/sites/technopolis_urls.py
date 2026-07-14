"""Technopolis product URL parsing and detection."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from app.utils.url_utils import is_technopolis, normalize_domain

_P_CODE_PATTERN = re.compile(r"/p/(\d+)(?:/|$|\?)", re.IGNORECASE)
_P_IN_PATH = re.compile(r"/p/\d+", re.IGNORECASE)
_LOCALE_PREFIX = re.compile(r"^/(bg|en)/", re.IGNORECASE)
_LEGACY_HTML_PRODUCT = re.compile(r"-\d+\.html$", re.IGNORECASE)

_TRACKING_QUERY_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref", "source")
_TRACKING_QUERY_EXACT = frozenset({"fbclid", "gclid", "ref", "source", "campaign", "affiliate"})


def is_technopolis_product_url(url: str) -> bool:
    """True for Technopolis PDP URLs on ``technopolis.bg`` with ``/p/{code}`` in the path."""
    if not is_technopolis(url):
        return False
    path = urlparse(url).path
    if _P_IN_PATH.search(path):
        return True
    return bool(_LEGACY_HTML_PRODUCT.search(path))


def is_technopolis_product_detail_url(url: str) -> bool:
    """Alias for :func:`is_technopolis_product_url`."""
    return is_technopolis_product_url(url)


def _strip_tracking_query(query: str) -> str:
    if not query:
        return ""
    params = parse_qs(query, keep_blank_values=False)
    kept = {
        k: v
        for k, v in params.items()
        if k.lower() not in _TRACKING_QUERY_EXACT
        and not any(k.lower().startswith(p) for p in _TRACKING_QUERY_PREFIXES)
    }
    return urlencode(kept, doseq=True)


def normalize_technopolis_product_url(url: str) -> str | None:
    """
    Canonical Technopolis product URL: same host, no fragment, no tracking query params.

    Returns ``None`` when the URL is not a product page.
    """
    if not is_technopolis_product_url(url):
        return None

    parsed = urlparse(url.strip())
    host = normalize_domain(parsed.netloc or "")
    if host != "technopolis.bg":
        return None

    path = parsed.path.rstrip("/") or parsed.path
    query = _strip_tracking_query(parsed.query)
    netloc = (parsed.netloc or "www.technopolis.bg").lower()
    return urlunparse((parsed.scheme or "https", netloc, path, "", query, ""))


def technopolis_product_code(url: str) -> str | None:
    """Extract ``/p/{code}`` product code when present."""
    if not url or not is_technopolis(url):
        return None
    m = _P_CODE_PATTERN.search(urlparse(url).path)
    return m.group(1) if m else None


def technopolis_url_locale(url: str) -> str | None:
    """Return ``bg`` or ``en`` when the path starts with a Technopolis locale segment."""
    if not is_technopolis(url):
        return None
    m = _LOCALE_PREFIX.match(urlparse(url).path)
    return m.group(1).lower() if m else None


def prefer_technopolis_product_url(existing: str, candidate: str) -> str:
    """Prefer ``/bg/`` product URLs over ``/en/`` for the same product code."""
    existing_locale = technopolis_url_locale(existing)
    candidate_locale = technopolis_url_locale(candidate)
    if candidate_locale == "bg" and existing_locale != "bg":
        return candidate
    return existing


def product_url_dedupe_key(url: str) -> str | None:
    """Dedupe key preferring Technopolis product code over normalized URL."""
    norm = normalize_technopolis_product_url(url)
    if not norm:
        return None
    code = technopolis_product_code(norm)
    return f"p:{code}" if code else f"u:{norm}"


def slug_to_display_name(slug: str) -> str:
    t = slug.replace("-", " ").replace("_", " ").strip()
    return re.sub(r"\s+", " ", t).title() if t else slug


def parse_technopolis_product_url(url: str) -> dict[str, Any] | None:
    """
    Parse ``/bg/{category_slug}/…/{product_slug}/p/{code}`` Technopolis product URLs.

    Returns dict with ``url_category_slug``, ``url_product_slug``, ``technopolis_product_code``,
    and ``url_path_segments`` (segments between ``bg`` and ``p``).
    """
    if not is_technopolis(url):
        return None

    path = urlparse(url).path.strip("/")
    segs = [s for s in path.split("/") if s]
    if not segs or segs[0].lower() not in ("bg", "en"):
        return None

    if "p" not in segs:
        return None

    p_idx = segs.index("p")
    if p_idx + 1 >= len(segs):
        return None

    code = segs[p_idx + 1]
    if not code.isdigit():
        return None

    between = segs[1:p_idx]
    if not between:
        return None

    category_slug = between[0]
    product_slug = between[-1] if len(between) >= 2 else between[0]

    return {
        "url_category_slug": category_slug,
        "url_product_slug": product_slug,
        "technopolis_product_code": code,
        "url_path_segments": between,
    }


def extract_url_metadata(url: str) -> dict[str, Any]:
    """Convenience wrapper for scraper ``raw_data`` fields."""
    parsed = parse_technopolis_product_url(url)
    if not parsed:
        return {}
    return {
        "url_category_slug": parsed["url_category_slug"],
        "url_product_slug": parsed["url_product_slug"],
        "technopolis_product_code": parsed["technopolis_product_code"],
    }
