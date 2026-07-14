"""Competitor site category (navigation / PLP tree)."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base_model import TimestampMixin

if TYPE_CHECKING:
    from app.models.competitor import Competitor
    from app.models.competitor_product import CompetitorProduct


class CompetitorCategory(Base, TimestampMixin):
    __tablename__ = "competitor_categories"

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
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("competitor_categories.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    product_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    parent: Mapped["CompetitorCategory | None"] = relationship(
        "CompetitorCategory",
        remote_side="CompetitorCategory.id",
        back_populates="children",
    )
    children: Mapped[list["CompetitorCategory"]] = relationship(
        "CompetitorCategory",
        back_populates="parent",
        foreign_keys="CompetitorCategory.parent_id",
    )

    competitor: Mapped["Competitor"] = relationship(
        "Competitor",
        back_populates="categories",
    )
    competitor_products: Mapped[list["CompetitorProduct"]] = relationship(
        "CompetitorProduct",
        back_populates="competitor_category",
    )
