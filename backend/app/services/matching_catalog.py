"""Narrow catalog product candidates before fuzzy matching."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CompetitorProduct, Product

CANDIDATE_CAP = 2000
PREFILTER_LIMIT = 500


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    s = value.strip()
    return s if s else None


def _add_unique(products: list[Product], seen: set[uuid.UUID], rows: list[Product]) -> None:
    for p in rows:
        if p.id not in seen:
            seen.add(p.id)
            products.append(p)
        if len(products) >= CANDIDATE_CAP:
            return


def fetch_catalog_candidates_for_listing(
    db: Session,
    cp: CompetitorProduct,
) -> list[Product] | None:
    """
    Return a bounded candidate list when identifier filters apply.

    Returns ``None`` when no strong filters matched — caller should fall back to batched full scan.
    """
    candidates: list[Product] = []
    seen: set[uuid.UUID] = set()
    had_filter = False

    ean = _norm(cp.ean)
    if ean:
        had_filter = True
        _add_unique(
            candidates,
            seen,
            list(db.scalars(select(Product).where(Product.ean == ean).limit(PREFILTER_LIMIT)).all()),
        )

    mfr = _norm(cp.manufacturer_code)
    if mfr:
        had_filter = True
        _add_unique(
            candidates,
            seen,
            list(
                db.scalars(
                    select(Product)
                    .where(Product.manufacturer_code == mfr)
                    .limit(PREFILTER_LIMIT),
                ).all(),
            ),
        )

    brand = _norm(cp.brand)
    model = _norm(cp.model)
    if brand and model:
        had_filter = True
        _add_unique(
            candidates,
            seen,
            list(
                db.scalars(
                    select(Product)
                    .where(
                        func.lower(Product.brand) == brand.lower(),
                        func.lower(Product.model) == model.lower(),
                    )
                    .limit(PREFILTER_LIMIT),
                ).all(),
            ),
        )
    elif brand:
        had_filter = True
        _add_unique(
            candidates,
            seen,
            list(
                db.scalars(
                    select(Product)
                    .where(func.lower(Product.brand) == brand.lower())
                    .limit(PREFILTER_LIMIT),
                ).all(),
            ),
        )

    sku = _norm(cp.sku)
    if sku and len(candidates) < CANDIDATE_CAP:
        had_filter = True
        _add_unique(
            candidates,
            seen,
            list(db.scalars(select(Product).where(Product.sku == sku).limit(50)).all()),
        )

    if not had_filter:
        return None
    return candidates


def iter_catalog_batches(db: Session, *, batch_size: int = 500):
    """Yield full catalog in stable ID order."""
    offset = 0
    while True:
        products = list(
            db.scalars(select(Product).order_by(Product.id).offset(offset).limit(batch_size)).all(),
        )
        if not products:
            break
        yield products
        offset += batch_size
