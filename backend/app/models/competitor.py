"""Competitor retailer / site configuration."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base_model import TimestampMixin

if TYPE_CHECKING:
    from app.models.competitor_category import CompetitorCategory
    from app.models.competitor_product import CompetitorProduct


class Competitor(Base, TimestampMixin):
    __tablename__ = "competitors"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="BGN")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Per-site cap for the adaptive scrape concurrency controller.
    # NULL falls back to the global SCRAPE_*_CONCURRENCY_MAX setting.
    scrape_concurrency_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    competitor_products: Mapped[list["CompetitorProduct"]] = relationship(
        "CompetitorProduct",
        back_populates="competitor",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    categories: Mapped[list["CompetitorCategory"]] = relationship(
        "CompetitorCategory",
        back_populates="competitor",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
