"""Build competitor category trees from product breadcrumbs and URL slugs."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompetitorCategory, CompetitorProduct
from app.scrapers.sites.technopolis_categories import normalize_category_name
from app.scrapers.sites.technopolis_urls import slug_to_display_name

logger = logging.getLogger(__name__)

TECHNOPOLIS_BASE = "https://www.technopolis.bg"


def _parse_breadcrumb_item(raw: Any) -> dict[str, str | None] | None:
    """Convert one breadcrumb entry to ``{name, url}`` or skip invalid items."""
    try:
        if raw is None:
            return None
        if isinstance(raw, str):
            name = normalize_category_name(raw.strip())
            if not name:
                return None
            return {"name": name, "url": None}
        if isinstance(raw, dict):
            name = normalize_category_name(str(raw.get("name") or ""))
            if not name:
                return None
            url = raw.get("url")
            if url:
                url = str(url).split("#")[0].strip() or None
            else:
                url = None
            return {"name": name, "url": url}
        name_attr = getattr(raw, "name", None)
        url_attr = getattr(raw, "url", None)
        if name_attr is not None or url_attr is not None:
            name = normalize_category_name(str(name_attr or ""))
            if not name:
                return None
            url = str(url_attr).split("#")[0].strip() if url_attr else None
            return {"name": name, "url": url or None}
    except Exception:  # noqa: BLE001
        return None
    return None


def _normalize_breadcrumb_items(items: list[Any] | None) -> list[dict[str, str | None]]:
    """Accept dicts, strings, objects with name/url, or skip null/invalid entries."""
    out: list[dict[str, str | None]] = []
    if not items:
        return out
    for raw in items:
        parsed = _parse_breadcrumb_item(raw)
        if parsed is not None:
            out.append(parsed)
    return out


def _synthetic_category_url(slug_or_name: str) -> str:
    slug = slug_or_name.strip().strip("/")
    if slug.startswith("http"):
        return slug if slug.endswith("/") else f"{slug}/"
    slug = slug.replace(" ", "-").lower()
    return f"{TECHNOPOLIS_BASE}/bg/{slug}/"


def _get_or_create_category(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    name: str,
    url: str | None,
    parent_id: uuid.UUID | None,
    level: int,
    source: str,
) -> CompetitorCategory:
    display_name = normalize_category_name(name)
    cat_url = url or _synthetic_category_url(display_name)

    by_url = db.scalars(
        select(CompetitorCategory).where(
            CompetitorCategory.competitor_id == competitor_id,
            CompetitorCategory.url == cat_url,
        ),
    ).first()
    if by_url is not None:
        if parent_id is not None and by_url.parent_id is None:
            by_url.parent_id = parent_id
        if len(display_name) >= len(by_url.name or ""):
            by_url.name = display_name
        by_url.level = min(by_url.level, level) if by_url.level else level
        return by_url

    by_name = db.scalars(
        select(CompetitorCategory).where(
            CompetitorCategory.competitor_id == competitor_id,
            CompetitorCategory.name == display_name,
            CompetitorCategory.parent_id == parent_id,
        ),
    ).first()
    if by_name is not None:
        if cat_url and by_name.url != cat_url:
            by_name.url = cat_url
        return by_name

    row = CompetitorCategory(
        competitor_id=competitor_id,
        parent_id=parent_id,
        name=display_name,
        url=cat_url,
        level=level,
        path=f"{source}:{display_name}"[:1024],
        product_count=0,
    )
    db.add(row)
    db.flush()
    return row


def _path_items_from_slug(fallback_category_slug: str) -> list[dict[str, str | None]]:
    slug = fallback_category_slug.strip().strip("/")
    return [
        {
            "name": slug_to_display_name(slug),
            "url": _synthetic_category_url(slug),
        },
    ]


def _build_deepest_category(
    db: Session,
    *,
    competitor_id: uuid.UUID,
    path_items: list[dict[str, str | None]],
    source: str,
) -> CompetitorCategory | None:
    parent_id: uuid.UUID | None = None
    deepest: CompetitorCategory | None = None
    for level, item in enumerate(path_items):
        deepest = _get_or_create_category(
            db,
            competitor_id=competitor_id,
            name=item["name"] or "",
            url=item.get("url"),
            parent_id=parent_id,
            level=level,
            source=source,
        )
        parent_id = deepest.id
    return deepest


def _link_product_to_breadcrumb_category(
    db: Session,
    competitor_product: CompetitorProduct,
    deepest: CompetitorCategory,
) -> None:
    """Assign listing category only when discovery has not already set one."""
    existing_id = competitor_product.competitor_category_id
    if existing_id is None:
        competitor_product.competitor_category_id = deepest.id
        db.flush()
        from app.services.competitor_category_service import refresh_category_product_counts

        refresh_category_product_counts(db, competitor_product.competitor_id)
        return
    if existing_id != deepest.id:
        logger.info(
            "category_relink_skipped_existing_category competitor_product_id=%s "
            "existing_category_id=%s breadcrumb_category_id=%s",
            competitor_product.id,
            existing_id,
            deepest.id,
        )


def ensure_category_path_for_competitor_product(
    db: Session,
    competitor_product: CompetitorProduct,
    breadcrumb_categories: list[Any] | None,
    fallback_category_slug: str | None = None,
) -> CompetitorCategory | None:
    """
    Create/update breadcrumb ``CompetitorCategory`` rows for a product path.

    Does not overwrite ``competitor_category_id`` when already set (e.g. sitemap discovery).
    Returns the deepest breadcrumb category, or ``None`` when the path cannot be built.
    Never raises.
    """
    cp_id = competitor_product.id
    try:
        crumbs = _normalize_breadcrumb_items(breadcrumb_categories)
        path_items: list[dict[str, str | None]] = []

        if crumbs:
            path_items = crumbs
        elif fallback_category_slug:
            path_items = _path_items_from_slug(fallback_category_slug)
        else:
            return None

        source = "breadcrumb" if crumbs else "url_slug"
        deepest = _build_deepest_category(
            db,
            competitor_id=competitor_product.competitor_id,
            path_items=path_items,
            source=source,
        )
        if deepest is None:
            return None

        _link_product_to_breadcrumb_category(db, competitor_product, deepest)
        return deepest
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "category_path_update_failed competitor_product_id=%s error=%s",
            cp_id,
            exc,
        )
        if fallback_category_slug:
            try:
                path_items = _path_items_from_slug(fallback_category_slug)
                deepest = _build_deepest_category(
                    db,
                    competitor_id=competitor_product.competitor_id,
                    path_items=path_items,
                    source="url_slug_fallback",
                )
                if deepest is not None:
                    _link_product_to_breadcrumb_category(db, competitor_product, deepest)
                    return deepest
            except Exception as slug_exc:  # noqa: BLE001
                logger.info(
                    "category_path_update_failed competitor_product_id=%s error=%s",
                    cp_id,
                    slug_exc,
                )
        return None


def display_category_path(
    db: Session,
    cp: CompetitorProduct,
    *,
    assigned_path_cache: dict[uuid.UUID, list[str]] | None = None,
) -> list[str]:
    """
    Category path for UI: prefer OCC breadcrumb names, then breadcrumb category tree, then assigned category.
    """
    ri = cp.raw_identifiers if isinstance(cp.raw_identifiers, dict) else {}
    crumbs = ri.get("breadcrumb_categories")
    if isinstance(crumbs, list) and crumbs:
        normalized = _normalize_breadcrumb_items(crumbs)
        names = [item["name"] for item in normalized if item.get("name")]
        if names:
            return names

    bc_id = ri.get("breadcrumb_category_id")
    if bc_id:
        try:
            breadcrumb_uid = uuid.UUID(str(bc_id))
        except (ValueError, TypeError):
            breadcrumb_uid = None
        if breadcrumb_uid is not None:
            if assigned_path_cache is not None:
                cached = assigned_path_cache.get(breadcrumb_uid)
                if cached:
                    return cached
            path = category_path_names(db, breadcrumb_uid)
            if path:
                return path

    if cp.competitor_category_id:
        if assigned_path_cache is not None:
            cached = assigned_path_cache.get(cp.competitor_category_id)
            if cached:
                return cached
        path = category_path_names(db, cp.competitor_category_id)
        if path:
            return path

    return ["Uncategorized"]


def category_path_names(db: Session, category_id: uuid.UUID | None) -> list[str]:
    """
    Walk parent chain and return root → leaf category names.

    Safe for ``None``, missing rows, cycles, and empty names.
    """
    if category_id is None:
        return []

    names: list[str] = []
    seen: set[uuid.UUID] = set()
    current = db.get(CompetitorCategory, category_id)

    while current is not None and current.id not in seen:
        seen.add(current.id)
        label = (current.name or "").strip()
        if label:
            names.append(label)
        if current.parent_id is None:
            break
        current = db.get(CompetitorCategory, current.parent_id)

    names.reverse()
    return names
