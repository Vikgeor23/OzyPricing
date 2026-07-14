"""Manual or automatic link between our Product and a CompetitorProduct."""

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, Uuid, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base_model import TimestampMixin

if TYPE_CHECKING:
    from app.models.competitor_product import CompetitorProduct
    from app.models.product import Product


class ProductMatch(Base, TimestampMixin):
    __tablename__ = "product_matches"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "competitor_product_id",
            name="uq_product_competitor_product",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    competitor_product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("competitor_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    match_score: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    match_method: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="needs_review",
    )
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_warnings: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top_candidates: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    matched_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    product: Mapped["Product"] = relationship("Product", back_populates="matches")
    competitor_product: Mapped["CompetitorProduct"] = relationship(
        "CompetitorProduct",
        back_populates="matches",
    )
