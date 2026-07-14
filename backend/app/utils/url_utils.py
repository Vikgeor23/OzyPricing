"""URL and domain normalization helpers."""

from __future__ import annotations

from urllib.parse import urlparse

TECHNOPOLIS_DOMAIN = "technopolis.bg"
TECHNOPOLIS_DEFAULT_START_URL = "https://www.technopolis.bg/bg/"


def normalize_domain(value: str) -> str:
    """
    Extract a bare hostname from a domain string or full URL.

    Strips protocol, ``www.``, path, query, fragment; lowercases the result.
    """
    if not value:
        return ""
    raw = value.strip()
    if not raw:
        return ""

    if "://" in raw or raw.startswith("//"):
        if raw.startswith("//"):
            raw = "https:" + raw
        hostname = urlparse(raw).hostname or ""
    else:
        chunk = raw.split("?")[0].split("#")[0]
        hostname = chunk.split("/")[0].split(":")[0]

    host = (hostname or "").lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_url(value: str) -> str:
    """Ensure a string is a full URL with scheme (defaults to https)."""
    if not value:
        return ""
    raw = value.strip()
    if not raw:
        return ""

    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")

    parsed = urlparse(raw)
    if not parsed.netloc:
        return raw

    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{parsed.scheme}://{parsed.netloc}{path}{query}{fragment}"


def is_technopolis(value: str) -> bool:
    """True when ``value`` resolves to the Technopolis retailer domain."""
    if not value or not value.strip():
        return False
    return normalize_domain(value) == TECHNOPOLIS_DOMAIN


def technopolis_category_start_url(value: str | None = None) -> str:
    """
    Pick the Technopolis category-discovery entry URL.

    Defaults to ``https://www.technopolis.bg/bg/``. If ``value`` is a Technopolis
    URL with a non-root path, that URL is used instead.
    """
    if not value or not value.strip():
        return TECHNOPOLIS_DEFAULT_START_URL

    raw = value.strip()
    if not is_technopolis(raw):
        return TECHNOPOLIS_DEFAULT_START_URL

    candidate = normalize_url(raw)
    parsed = urlparse(candidate)
    if parsed.path and parsed.path not in ("", "/"):
        return candidate if candidate.endswith("/") else f"{candidate}/"

    return TECHNOPOLIS_DEFAULT_START_URL
