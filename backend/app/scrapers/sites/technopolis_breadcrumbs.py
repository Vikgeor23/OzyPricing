"""Extract category breadcrumbs from Technopolis product detail pages."""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.scrapers.sites.technopolis_categories import normalize_category_name

_BREADCRUMB_SELECTORS = (
    ".breadcrumb a",
    "nav.breadcrumb a",
    "ol.breadcrumb li a",
    "ul.breadcrumb li a",
    "[class*='breadcrumb'] a",
    "nav[aria-label*='breadcrumb' i] a",
)

_BREADCRUMB_CONTAINER_SELECTORS = (
    "[class*='breadcrumb']",
    ".breadcrumb",
    "nav[aria-label*='breadcrumb' i]",
    "ol.breadcrumb",
)

_HOME_LABELS = frozenset(
    {
        "начало",
        "home",
        "начална страница",
        "technopolis",
        "technopolis.bg",
    },
)


def extract_breadcrumb_categories(
    soup: BeautifulSoup,
    page_url: str,
    *,
    product_title: str | None = None,
) -> list[dict[str, str | None]]:
    """
    Return category crumbs from a PDP, excluding home and the final product title.

    Each item is ``{"name": "...", "url": "..."}`` (``url`` may be ``None``).
    """
    items: list[dict[str, str | None]] = []

    for sel in _BREADCRUMB_SELECTORS:
        nodes = soup.select(sel)
        if len(nodes) < 2:
            continue
        for node in nodes:
            text = normalize_category_name(node.get_text(" ", strip=True))
            if not text or text.lower() in _HOME_LABELS:
                continue
            href = node.get("href")
            url = urljoin(page_url, href).split("#")[0].strip() if href else None
            items.append({"name": text, "url": url or None})
        break

    if not items:
        for sel in _BREADCRUMB_CONTAINER_SELECTORS:
            container = soup.select_one(sel)
            if container is None:
                continue
            text = container.get_text(">", strip=True)
            parts = [normalize_category_name(p) for p in text.split(">") if p.strip()]
            for part in parts:
                if not part or part.lower() in _HOME_LABELS:
                    continue
                items.append({"name": part, "url": None})
            if items:
                break

    if not items:
        return []

    while items and _is_product_title_crumb(items[-1], product_title):
        items.pop()

    return [i for i in items if i.get("name")]


def _is_product_title_crumb(item: dict[str, str | None], product_title: str | None) -> bool:
    if not product_title:
        return False
    name = (item.get("name") or "").strip().lower()
    title = normalize_category_name(product_title).lower()
    if not name or not title:
        return False
    return name == title or title.startswith(name) or name in title
