"""Parse and normalize Bulgarian retail price strings (лв / BGN)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

LEV_SUFFIX = re.compile(
    r"(?P<num>[\d\s][\d\s.,]*)\s*(?:лв\.?|BGN)\b",
    re.IGNORECASE | re.MULTILINE,
)


def parse_bg_leva_amount(num_fragment: str) -> Decimal | None:
    """Normalize a numeric fragment like '1 299,00' or '299.99' to Decimal."""
    t = re.sub(r"\s+", "", num_fragment.strip())
    if not t:
        return None
    if not re.match(r"^[\d.,]+$", t):
        return None

    comma = t.count(",")
    dot = t.count(".")

    try:
        if comma == 0 and dot == 0:
            return Decimal(t)

        if comma >= 1 and dot == 0:
            parts = t.split(",")
            if len(parts) == 2 and len(parts[1]) <= 2 and parts[1].isdigit():
                whole = parts[0].replace(".", "")
                return Decimal(f"{whole}.{parts[1]}")
            return Decimal(t.replace(",", ""))

        if dot >= 1 and comma == 0:
            parts = t.split(".")
            if len(parts) == 2 and len(parts[-1]) <= 2 and parts[-1].isdigit():
                return Decimal(t)
            return Decimal(t.replace(".", ""))

        li, ri = t.rfind(","), t.rfind(".")
        if ri > li:
            whole, frac = t.rsplit(".", 1)
            whole = whole.replace(",", "").replace(".", "")
            return Decimal(f"{whole}.{frac}")
        whole, frac = t.rsplit(",", 1)
        whole = whole.replace(",", "").replace(".", "")
        return Decimal(f"{whole}.{frac}")
    except (InvalidOperation, ValueError):
        return None


def extract_all_leva_amounts(text: str) -> list[Decimal]:
    out: list[Decimal] = []
    for m in LEV_SUFFIX.finditer(text):
        p = parse_bg_leva_amount(m.group("num"))
        if p is not None:
            out.append(p)
    return out


def pick_likely_prices(
    candidates: list[Decimal],
    *,
    min_value: Decimal = Decimal("0.01"),
    max_value: Decimal = Decimal("999999.99"),
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """
    Return (likely_current, likely_old, promo_same_as_current).

    Heuristic: one value => current. Multiple => min ~ current/promo, max ~ old list price.
    """
    sane: list[Decimal] = [c for c in candidates if min_value <= c <= max_value]
    if not sane:
        return None, None, None
    uniq = sorted(set(sane))
    if len(uniq) == 1:
        return uniq[0], None, None
    low, high = uniq[0], uniq[-1]
    if low == high:
        return low, None, None
    return low, high, low
