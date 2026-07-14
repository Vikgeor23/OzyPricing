"""Confirm / reject ProductMatch rows."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompetitorProduct, ProductMatch
from app.schemas.match import MatchConfirmBody, MatchRejectBody


def upsert_match_and_link_product(db: Session, body: MatchConfirmBody) -> ProductMatch:
    cp = db.get(CompetitorProduct, body.competitor_product_id)
    if cp is None:
        msg = "Competitor product not found"
        raise ValueError(msg)

    stmt = select(ProductMatch).where(
        ProductMatch.product_id == body.product_id,
        ProductMatch.competitor_product_id == body.competitor_product_id,
    )
    row = db.scalars(stmt).first()
    if row:
        row.match_score = body.match_score
        row.match_method = body.match_method
        row.status = "confirmed"
    else:
        row = ProductMatch(
            product_id=body.product_id,
            competitor_product_id=body.competitor_product_id,
            match_score=body.match_score,
            match_method=body.match_method,
            status="confirmed",
        )
        db.add(row)

    cp.product_id = body.product_id
    db.commit()
    db.refresh(row)
    return row


def reject_match(db: Session, body: MatchRejectBody) -> ProductMatch:
    cp = db.get(CompetitorProduct, body.competitor_product_id)
    if cp is None:
        msg = "Competitor product not found"
        raise ValueError(msg)

    stmt = select(ProductMatch).where(
        ProductMatch.product_id == body.product_id,
        ProductMatch.competitor_product_id == body.competitor_product_id,
    )
    row = db.scalars(stmt).first()
    if row:
        row.status = "rejected"
    else:
        row = ProductMatch(
            product_id=body.product_id,
            competitor_product_id=body.competitor_product_id,
            match_score=Decimal("0"),
            match_method="manual_reject",
            status="rejected",
        )
        db.add(row)

    if cp.product_id == body.product_id:
        cp.product_id = None

    db.commit()
    db.refresh(row)
    return row
