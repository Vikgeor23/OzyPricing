#!/usr/bin/env python3
"""
Print EXPLAIN ANALYZE for core Price Monitor queries.

Usage (from repo root, with Postgres running):
  docker compose run --rm backend python scripts/explain_core_queries.py

Or locally:
  cd backend && python scripts/explain_core_queries.py
"""

from __future__ import annotations

import os
import sys
import uuid

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, aliased

# Allow running as `python scripts/explain_core_queries.py` from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.latest_price import best_match_subquery, latest_price_subquery
from app.models import CompetitorProduct, Product, ProductMatch
from app.services.matching_catalog import fetch_catalog_candidates_for_listing
from app.services.workspace_query import (
    WorkspaceQueryParams,
    list_category_workspace_page,
    list_competitor_workspace_page,
)


def _database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:postgres@localhost:5432/pricing_monitor",
    )


def explain(session: Session, label: str, stmt) -> None:
    print("\n" + "=" * 72)
    print(label)
    print("=" * 72)
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    try:
        rows = session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}")).all()
        for row in rows:
            print(row[0])
    except Exception as exc:  # noqa: BLE001
        print(f"EXPLAIN failed: {exc}")
        print("SQL:", sql[:500], "..." if len(sql) > 500 else "")


def main() -> None:
    engine = create_engine(_database_url())
    with Session(engine) as db:
        category_id = db.scalar(
            text("SELECT id FROM competitor_categories ORDER BY created_at LIMIT 1"),
        )
        competitor_id = db.scalar(
            text("SELECT id FROM competitors ORDER BY created_at LIMIT 1"),
        )
        cp = db.scalar(select(CompetitorProduct).limit(1))
        product = db.scalar(select(Product).limit(1))

        if category_id:
            page = list_category_workspace_page(
                db,
                uuid.UUID(str(category_id)),
                WorkspaceQueryParams(limit=75, offset=0),
            )
            if page:
                latest = latest_price_subquery()
                best = best_match_subquery()
                product_direct = aliased(Product)
                product_match = aliased(Product)
                stmt = (
                    select(CompetitorProduct)
                    .outerjoin(latest, latest.c.competitor_product_id == CompetitorProduct.id)
                    .outerjoin(best, best.c.competitor_product_id == CompetitorProduct.id)
                    .outerjoin(product_direct, product_direct.id == CompetitorProduct.product_id)
                    .outerjoin(product_match, product_match.id == best.c.product_id)
                    .where(CompetitorProduct.competitor_category_id == uuid.UUID(str(category_id)))
                    .order_by(CompetitorProduct.created_at.desc())
                    .limit(75)
                )
                explain(db, "Competitor category product page (workspace)", stmt)
        else:
            print("Skip category workspace: no categories in DB")

        if competitor_id:
            latest = latest_price_subquery()
            best = best_match_subquery()
            stmt = (
                select(CompetitorProduct)
                .outerjoin(latest, latest.c.competitor_product_id == CompetitorProduct.id)
                .outerjoin(best, best.c.competitor_product_id == CompetitorProduct.id)
                .where(CompetitorProduct.competitor_id == uuid.UUID(str(competitor_id)))
                .order_by(CompetitorProduct.created_at.desc())
                .limit(75)
            )
            explain(db, "Competitor all products page (workspace)", stmt)
            _ = list_competitor_workspace_page(
                db,
                uuid.UUID(str(competitor_id)),
                WorkspaceQueryParams(limit=75, offset=0),
            )
        else:
            print("Skip competitor workspace: no competitors in DB")

        if product:
            from app.services.price_comparison_service import build_price_comparison_page

            build_price_comparison_page(db, limit=75, offset=0)
            latest = latest_price_subquery()
            stmt = (
                select(latest)
                .where(
                    latest.c.competitor_product_id.in_(
                        select(ProductMatch.competitor_product_id).where(
                            ProductMatch.product_id == product.id,
                        ),
                    ),
                )
            )
            explain(db, "Products price comparison — latest prices for linked listings", stmt)

            first_cp_id = db.scalar(select(CompetitorProduct.id).limit(1))
            if first_cp_id:
                explain(
                    db,
                    "Latest price lookup (single listing subquery)",
                    select(latest).where(latest.c.competitor_product_id == first_cp_id),
                )
        else:
            print("Skip price comparison: no products in DB")

        if cp:
            candidates = fetch_catalog_candidates_for_listing(db, cp)
            if candidates is not None and cp.ean:
                stmt = select(Product).where(Product.ean == cp.ean).limit(500)
                explain(db, "Match candidate lookup (EAN prefilter)", stmt)
            elif candidates is not None and cp.manufacturer_code:
                stmt = select(Product).where(Product.manufacturer_code == cp.manufacturer_code).limit(500)
                explain(db, "Match candidate lookup (manufacturer code prefilter)", stmt)
            else:
                stmt = select(Product).order_by(Product.id).limit(500)
                explain(db, "Match candidate lookup (catalog batch scan)", stmt)
        else:
            print("Skip match candidates: no competitor products in DB")

    print("\nDone.")


if __name__ == "__main__":
    main()
