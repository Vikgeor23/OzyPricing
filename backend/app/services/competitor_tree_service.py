"""Build competitor → category tree responses."""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Competitor, CompetitorCategory
from app.schemas.competitor_tree import CategoryTreeNode, CompetitorTreeItem


def build_competitor_forest(db: Session) -> list[CompetitorTreeItem]:
    competitors = list(db.scalars(select(Competitor).order_by(Competitor.name)).all())
    out: list[CompetitorTreeItem] = []

    for comp in competitors:
        cats = list(
            db.scalars(
                select(CompetitorCategory).where(CompetitorCategory.competitor_id == comp.id),
            ).all(),
        )
        by_parent: dict[uuid.UUID | None, list[CompetitorCategory]] = defaultdict(list)
        for c in cats:
            by_parent[c.parent_id].append(c)

        for lst in by_parent.values():
            lst.sort(key=lambda x: (x.level, x.name.lower()))

        memo: dict[uuid.UUID, CategoryTreeNode] = {}

        def build_node(row: CompetitorCategory) -> CategoryTreeNode:
            if row.id in memo:
                return memo[row.id]
            ch = [build_node(x) for x in by_parent.get(row.id, [])]
            node = CategoryTreeNode(
                id=row.id,
                parent_id=row.parent_id,
                name=row.name,
                url=row.url,
                level=row.level,
                product_count=row.product_count,
                children=ch,
            )
            memo[row.id] = node
            return node

        roots = [build_node(r) for r in by_parent.get(None, [])]
        out.append(
            CompetitorTreeItem(
                id=comp.id,
                name=comp.name,
                domain=comp.domain,
                country=comp.country,
                currency=comp.currency,
                scrape_concurrency_max=comp.scrape_concurrency_max,
                categories=roots,
            ),
        )

    return out
