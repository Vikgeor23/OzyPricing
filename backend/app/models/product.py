"""Product model — merchant's own catalog items."""

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base_model import TimestampMixin

if TYPE_CHECKING:
    from app.models.competitor_product import CompetitorProduct
    from app.models.product_match import ProductMatch


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        index=True,
    )
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    sku: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    ean: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manufacturer_code: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    own_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    stock_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    product_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    variant: Mapped[str | None] = mapped_column(String(255), nullable=True)
    color: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    memory: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supplier_sku: Mapped[str | None] = mapped_column(String(255), nullable=True)

    competitor_products: Mapped[list["CompetitorProduct"]] = relationship(
        "CompetitorProduct",
        back_populates="product",
    )
    matches: Mapped[list["ProductMatch"]] = relationship(
        "ProductMatch",
        back_populates="product",
    )
