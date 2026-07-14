"""Helpers to expose ProductMatch metadata on workspace rows."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.schemas.match import MatchCandidate


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def parse_top_candidates(raw: Any) -> list[MatchCandidate]:
    if not isinstance(raw, list):
        return []
    out: list[MatchCandidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                MatchCandidate(
                    product_id=uuid.UUID(str(item["product_id"])),
                    sku=str(item.get("sku") or ""),
                    name=str(item.get("name") or ""),
                    brand=item.get("brand"),
                    ean=item.get("ean"),
                    manufacturer_code=item.get("manufacturer_code"),
                    model=item.get("model"),
                    image_url=item.get("image_url"),
                    own_price=_decimal_or_none(item.get("own_price")),
                    match_score=_decimal_or_none(item.get("match_score")) or Decimal("0"),
                    match_method=str(item.get("match_method") or "unknown"),
                    match_reasons=list(item.get("match_reasons") or []),
                    match_warnings=list(item.get("match_warnings") or []),
                    suggested_status=str(item.get("suggested_status") or "no_match"),
                ),
            )
        except Exception:  # noqa: BLE001
            continue
    return out


def parse_match_warnings(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x is not None and str(x).strip()]
