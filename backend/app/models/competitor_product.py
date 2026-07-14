"""A competitor listing URL linked optionally to our Product."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base_model import TimestampMixin

if TYPE_CHECKING:
    from app.models.competitor import Competitor
    from app.models.competitor_category import CompetitorCategory
    from app.models.price_snapshot import PriceSnapshot
    from app.models.product import Product
    from app.models.product_match import ProductMatch


class CompetitorProduct(Base, TimestampMixin):
    __tablename__ = "competitor_products"
    __table_args__ = (
        UniqueConstraint("competitor_id", "url", name="uq_competitor_product_url"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    competitor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("competitors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    competitor_category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("competitor_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ean: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    manufacturer_code: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The retailer's own internal code, kept when a supplier/article number
    # displaces it from `sku`; extra_code holds any additional identifier the
    # site exposes under shop-specific attribute names.
    shop_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    specs_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_identifiers: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    latest_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    latest_old_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    latest_promo_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    latest_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    latest_availability: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Marketplace seller info (eMAG): "Предлаган от" / "Доставка от".
    latest_offered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    latest_delivered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    latest_scraped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    latest_scrape_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latest_scrape_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_scrape_error_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    discovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    discovery_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    technopolis_product_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )
    is_dead: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    consecutive_timeout_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    consecutive_not_found_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    competitor: Mapped["Competitor"] = relationship(
        "Competitor",
        back_populates="competitor_products",
    )
    competitor_category: Mapped["CompetitorCategory | None"] = relationship(
        "CompetitorCategory",
        back_populates="competitor_products",
    )
    product: Mapped["Product | None"] = relationship(
        "Product",
        back_populates="competitor_products",
    )
    price_snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        "PriceSnapshot",
        back_populates="competitor_product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # Owned by the competitor_product; the DB FK is ON DELETE CASCADE, so let the
    # database clean them up (passive_deletes) instead of SQLAlchemy trying to
    # NULL out the NOT NULL competitor_product_id, which raised on delete.
    matches: Mapped[list["ProductMatch"]] = relationship(
        "ProductMatch",
        back_populates="competitor_product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
