"""CRUD and queries for products."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db.latest_price import latest_price_subquery
from app.services.listing_price import price_snapshot_read_for_listing
from app.db.pagination import clamp_limit, normalize_offset
from app.models import CompetitorProduct, Product, ProductMatch
from app.schemas.price import PriceSnapshotRead, ProductPriceRow, ProductPricesResponse
from app.schemas.product import ProductCreate, ProductListPage, ProductRead, ProductUpdate


def list_products(db: Session) -> list[Product]:
    stmt = select(Product).order_by(Product.created_at.desc())
    return list(db.scalars(stmt).all())


def list_products_page(db: Session, *, limit: int = 75, offset: int = 0) -> ProductListPage:
    limit = clamp_limit(limit)
    offset = normalize_offset(offset)
    total = int(db.scalar(select(func.count()).select_from(Product)) or 0)
    rows = list(
        db.scalars(
            select(Product).order_by(Product.created_at.desc()).limit(limit).offset(offset),
        ).all(),
    )
    items = [ProductRead.model_validate(p) for p in rows]
    return ProductListPage(
        rows=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(items)) < total,
    )


def get_product(db: Session, product_id: uuid.UUID) -> Product | None:
    return db.get(Product, product_id)


def create_product(db: Session, data: ProductCreate) -> Product:
    product = Product(**data.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def update_product(db: Session, product: Product, data: ProductUpdate) -> Product:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(product, key, value)
    db.commit()
    db.refresh(product)
    return product


def delete_product(db: Session, product: Product) -> None:
    db.delete(product)
    db.commit()


def _linked_competitor_product_ids(db: Session, product_id: uuid.UUID) -> list[uuid.UUID]:
    direct_stmt = select(CompetitorProduct.id).where(CompetitorProduct.product_id == product_id)
    match_stmt = select(ProductMatch.competitor_product_id).where(
        ProductMatch.product_id == product_id,
    )
    direct_ids = set(db.scalars(direct_stmt).all())
    match_ids = set(db.scalars(match_stmt).all())
    return list(direct_ids | match_ids)


def get_product_prices(db: Session, product_id: uuid.UUID) -> ProductPricesResponse | None:
    product = get_product(db, product_id)
    if product is None:
        return None

    cp_ids = _linked_competitor_product_ids(db, product_id)
    if not cp_ids:
        return ProductPricesResponse(product_id=product_id, rows=[])

    cps = list(
        db.scalars(
            select(CompetitorProduct)
            .where(CompetitorProduct.id.in_(cp_ids))
            .options(joinedload(CompetitorProduct.competitor)),
        ).all(),
    )
    latest_rows: dict = {}
    if get_settings().price_history_enabled:
        latest = latest_price_subquery()
        latest_rows = {
            r.competitor_product_id: r
            for r in db.execute(
                select(latest).where(latest.c.competitor_product_id.in_(cp_ids)),
            ).all()
        }

    rows: list[ProductPriceRow] = []
    for cp in cps:
        snap_row = latest_rows.get(cp.id)
        comp = cp.competitor
        latest_read = price_snapshot_read_for_listing(cp, snap_row=snap_row)
        rows.append(
            ProductPriceRow(
                competitor_product_id=cp.id,
                competitor_id=comp.id,
                competitor_name=comp.name,
                competitor_domain=comp.domain,
                listing_url=cp.url,
                listing_title=cp.title,
                latest_snapshot=latest_read,
            ),
        )

    return ProductPricesResponse(product_id=product_id, rows=rows)
