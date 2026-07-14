"""Historical price capture for a competitor product listing."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.competitor_product import CompetitorProduct


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    competitor_product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("competitor_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    promo_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="BGN")
    availability: Mapped[str | None] = mapped_column(String(128), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    raw_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    competitor_product: Mapped["CompetitorProduct"] = relationship(
        "CompetitorProduct",
        back_populates="price_snapshots",
    )
