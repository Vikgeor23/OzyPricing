"""Paginated dashboard rows (batched latest prices, no N+1)."""

from datetime import datetime
from decimal import Decimal
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.latest_price import latest_price_subquery
from app.db.pagination import clamp_limit, normalize_offset
from app.models import CompetitorProduct, Product, ProductMatch


class DashboardProductRow(BaseModel):
    product_id: uuid.UUID
    name: str
    sku: str
    own_price: Decimal | None
    lowest_competitor_price: Decimal | None
    difference_percent: Decimal | None = Field(
        None,
        description="(own_price - lowest) / own_price * 100 when both present",
    )
    last_checked_at: datetime | None


class DashboardProductsPage(BaseModel):
    rows: list[DashboardProductRow] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    has_more: bool


def _batch_cp_ids_for_products(db: Session, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, set[uuid.UUID]]:
    result = {pid: set() for pid in product_ids}
    if not product_ids:
        return result
    for pid, cp_id in db.execute(
        select(CompetitorProduct.product_id, CompetitorProduct.id).where(
            CompetitorProduct.product_id.in_(product_ids),
        ),
    ):
        result[pid].add(cp_id)
    for pid, cp_id in db.execute(
        select(ProductMatch.product_id, ProductMatch.competitor_product_id).where(
            ProductMatch.product_id.in_(product_ids),
        ),
    ):
        result[pid].add(cp_id)
    return result


def _effective_price_from_row(row) -> Decimal | None:
    if row is None:
        return None
    if row.promo_price is not None:
        return row.promo_price
    return row.price


def build_dashboard_page(db: Session, *, limit: int = 75, offset: int = 0) -> DashboardProductsPage:
    limit = clamp_limit(limit)
    offset = normalize_offset(offset)
    total = int(db.scalar(select(func.count()).select_from(Product)) or 0)
    products = list(
        db.scalars(select(Product).order_by(Product.name).limit(limit).offset(offset)).all(),
    )

    if not products:
        return DashboardProductsPage(rows=[], total=total, limit=limit, offset=offset, has_more=False)

    product_ids = [p.id for p in products]
    links = _batch_cp_ids_for_products(db, product_ids)
    all_cp_ids = set()
    for pid in product_ids:
        all_cp_ids |= links[pid]

    latest_by_cp: dict[uuid.UUID, object] = {}
    if all_cp_ids:
        latest = latest_price_subquery()
        latest_by_cp = {
            r.competitor_product_id: r
            for r in db.execute(
                select(latest).where(latest.c.competitor_product_id.in_(list(all_cp_ids))),
            ).all()
        }

    rows: list[DashboardProductRow] = []
    for product in products:
        lowest: Decimal | None = None
        last_checked: datetime | None = None
        for cp_id in links[product.id]:
            snap = latest_by_cp.get(cp_id)
            if snap is None:
                continue
            eff = _effective_price_from_row(snap)
            if eff is not None and (lowest is None or eff < lowest):
                lowest = eff
            if snap.captured_at and (last_checked is None or snap.captured_at > last_checked):
                last_checked = snap.captured_at

        diff_pct: Decimal | None = None
        if product.own_price is not None and lowest is not None and product.own_price != 0:
            diff_pct = (product.own_price - lowest) / product.own_price * Decimal("100")

        rows.append(
            DashboardProductRow(
                product_id=product.id,
                name=product.name,
                sku=product.sku,
                own_price=product.own_price,
                lowest_competitor_price=lowest,
                difference_percent=diff_pct,
                last_checked_at=last_checked,
            ),
        )

    return DashboardProductsPage(
        rows=rows,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(rows)) < total,
    )


def build_dashboard_rows(db: Session) -> list[DashboardProductRow]:
    page = build_dashboard_page(db, limit=75, offset=0)
    return list(page.rows)
