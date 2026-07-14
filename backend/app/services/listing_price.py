"""Read effective listing price / scrape time from CompetitorProduct latest_* fields."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.config import get_settings
from app.models import CompetitorProduct
from app.schemas.price import PriceSnapshotRead


def effective_listing_price(cp: CompetitorProduct) -> Decimal | None:
    if cp.latest_promo_price is not None:
        return cp.latest_promo_price
    if cp.latest_price is not None:
        return cp.latest_price
    return None


def listing_currency(cp: CompetitorProduct) -> str:
    return (cp.latest_currency or "BGN") or "BGN"


def listing_last_scraped_at(cp: CompetitorProduct) -> datetime | None:
    return cp.latest_scraped_at


def listing_last_checked_at(cp: CompetitorProduct) -> datetime | None:
    return cp.latest_scraped_at or cp.last_seen_at


def is_listing_scraped(cp: CompetitorProduct) -> bool:
    return cp.latest_scraped_at is not None or effective_listing_price(cp) is not None


def price_snapshot_read_for_listing(
    cp: CompetitorProduct,
    *,
    snap_row=None,
) -> PriceSnapshotRead | None:
    """Build display DTO from latest_* fields, optional legacy snapshot row fallback."""
    eff = effective_listing_price(cp)
    captured = listing_last_checked_at(cp)
    if eff is None and captured is None and snap_row is None:
        return None

    if eff is not None or cp.latest_scraped_at is not None:
        return PriceSnapshotRead(
            id=snap_row.id if snap_row is not None else cp.id,
            competitor_product_id=cp.id,
            price=cp.latest_price,
            old_price=cp.latest_old_price,
            promo_price=cp.latest_promo_price,
            currency=listing_currency(cp),
            availability=cp.latest_availability,
            captured_at=cp.latest_scraped_at or cp.last_seen_at or datetime.now(timezone.utc),
            raw_data=None,
        )

    if snap_row is not None:
        return PriceSnapshotRead(
            id=snap_row.id,
            competitor_product_id=snap_row.competitor_product_id,
            price=snap_row.price,
            old_price=snap_row.old_price,
            promo_price=snap_row.promo_price,
            currency=snap_row.currency or "BGN",
            availability=snap_row.availability,
            captured_at=snap_row.captured_at,
            raw_data=None,
        )
    return None


def competitor_price_from_listing(
    cp: CompetitorProduct,
    *,
    snap_row=None,
) -> tuple[Decimal | None, str, str | None, datetime | None]:
    """Return (effective_price, currency, availability, last_checked) for comparison rows."""
    eff = effective_listing_price(cp)
    currency = listing_currency(cp)
    availability = cp.latest_availability
    last_checked = listing_last_checked_at(cp)

    if eff is None and snap_row is not None and get_settings().price_history_enabled:
        eff = snap_row.promo_price if snap_row.promo_price is not None else snap_row.price
        currency = (snap_row.currency or "BGN") or "BGN"
        availability = snap_row.availability
        last_checked = snap_row.captured_at

    return eff, currency, availability, last_checked
