"""Deterministic scoring of catalog products against competitor listings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from app.models import CompetitorProduct, Product

# Threshold bands (suggestions only — never auto-confirm)
THRESHOLD_AUTO = Decimal("95")
THRESHOLD_REVIEW = Decimal("80")
THRESHOLD_WEAK = Decimal("60")


@dataclass
class MatchEvaluation:
    score: Decimal
    method: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggested_status: str = "no_match"


def _norm(x: str | None) -> str:
    return (x or "").strip().lower()


def _norm_code(x: str | None) -> str:
    return re.sub(r"[\s\-_./]", "", _norm(x))


def _norm_ean(x: str | None) -> str | None:
    if not x:
        return None
    digits = re.sub(r"\D", "", x)
    return digits if len(digits) >= 8 else None


def _listing_text_blob(cp: CompetitorProduct) -> str:
    parts = [cp.title or "", cp.brand or "", cp.model or ""]
    specs = cp.specs_json if isinstance(cp.specs_json, dict) else {}
    parts.extend(str(v) for v in specs.values())
    return " ".join(parts).lower()


def _extract_attr(text: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).strip().lower()
    return None


def _storage_from_product(p: Product) -> str | None:
    for field_name in ("storage",):
        val = getattr(p, field_name, None)
        if val:
            return _norm(val)
    return None


def _storage_from_specs(cp: CompetitorProduct) -> str | None:
    specs = cp.specs_json if isinstance(cp.specs_json, dict) else {}
    for key in ("storage", "памет", "capacity"):
        if key in specs:
            return _norm(str(specs[key]))
    return _extract_attr(_listing_text_blob(cp), [r"(\d+)\s*gb", r"(\d+)\s*tb"])


def _color_from_product(p: Product) -> str | None:
    return _norm(p.color) if getattr(p, "color", None) else None


def _color_from_specs(cp: CompetitorProduct) -> str | None:
    specs = cp.specs_json if isinstance(cp.specs_json, dict) else {}
    for key in ("color", "colour", "цвят"):
        if key in specs:
            return _norm(str(specs[key]))
    return None


def _memory_from_product(p: Product) -> str | None:
    return _norm(p.memory) if getattr(p, "memory", None) else None


def _memory_from_specs(cp: CompetitorProduct) -> str | None:
    specs = cp.specs_json if isinstance(cp.specs_json, dict) else {}
    for key in ("memory", "ram", "оперативна памет"):
        if key in specs:
            return _norm(str(specs[key]))
    return _extract_attr(_listing_text_blob(cp), [r"(\d+)\s*gb\s*ram", r"(\d+)\s*gb\s*ddr"])


def _apply_attribute_adjustments(
    evaln: MatchEvaluation,
    product: Product,
    cp: CompetitorProduct,
) -> None:
    p_storage = _storage_from_product(product)
    c_storage = _storage_from_specs(cp)
    p_color = _color_from_product(product)
    c_color = _color_from_specs(cp)
    p_memory = _memory_from_product(product)
    c_memory = _memory_from_specs(cp)

    if p_storage and c_storage:
        if p_storage == c_storage or p_storage in c_storage or c_storage in p_storage:
            evaln.reasons.append(f"Storage matches: {product.storage or c_storage}")
            evaln.score = min(Decimal("100"), evaln.score + Decimal("2"))
        else:
            evaln.warnings.append(f"Storage differs: catalog {product.storage or p_storage} vs listing {c_storage}")
            evaln.score = max(Decimal("0"), evaln.score - Decimal("15"))

    if p_color and c_color:
        if p_color == c_color:
            evaln.reasons.append(f"Color matches: {product.color or c_color}")
            evaln.score = min(Decimal("100"), evaln.score + Decimal("1"))
        else:
            evaln.warnings.append(f"Color differs: catalog {product.color or p_color} vs listing {c_color}")
            evaln.score = max(Decimal("0"), evaln.score - Decimal("12"))

    if p_memory and c_memory:
        if p_memory == c_memory or p_memory in c_memory or c_memory in p_memory:
            evaln.reasons.append(f"Memory matches: {product.memory or c_memory}")
            evaln.score = min(Decimal("100"), evaln.score + Decimal("1"))
        else:
            evaln.warnings.append(f"Memory differs: catalog {product.memory or p_memory} vs listing {c_memory}")
            evaln.score = max(Decimal("0"), evaln.score - Decimal("8"))



# Product-type keywords (BG + EN). Two names that both declare a type from
# this vocabulary but share none of them describe different kinds of products
# — a "пъзел" is never a "часовник", no matter how similar the license text.
_TYPE_KEYWORDS = frozenset(
    {
        "пъзел", "пъзели", "конструктор", "конструктори", "фигура", "фигурка", "фигури",
        "кукла", "кукли", "книга", "книжка", "книжки", "часовник", "игра", "игри",
        "раница", "чанта", "портмоне", "несесер", "колело", "тротинетка", "скутер",
        "плюшена", "плюшено", "плюш", "топка", "робот", "дрон", "количка", "влак",
        "самолет", "костюм", "рокля", "панталон", "тениска", "блуза", "яке", "пижама",
        "суитшърт", "клин", "обувки", "маратонки", "чехли", "сандали", "шапка",
        "чорапи", "бельо", "боди", "гащеризон", "парфюм", "крем", "шампоан", "лосион",
        "гел", "спирала", "червило", "балсам", "четка", "лаптоп", "телефон",
        "смартфон", "таблет", "монитор", "телевизор", "слушалки", "тонколона",
        "мишка", "клавиатура", "принтер", "рутер", "камера", "хладилник", "пералня",
        "фурна", "микровълнова", "прахосмукачка", "ютия", "кафемашина", "климатик",
        "puzzle", "watch", "figure", "doll", "book", "backpack", "scooter", "laptop",
        "phone", "tablet", "headphones", "keyboard", "mouse", "monitor", "plush",
    },
)


def _type_tokens(text: str | None) -> set[str]:
    return {tok for tok in re.findall(r"[a-zа-я]+", _norm(text)) if tok in _TYPE_KEYWORDS}


def _finalize_status(evaln: MatchEvaluation) -> MatchEvaluation:
    if evaln.score >= THRESHOLD_AUTO:
        evaln.suggested_status = "auto_match"
    elif evaln.score >= THRESHOLD_REVIEW:
        evaln.suggested_status = "needs_review"
    elif evaln.score >= THRESHOLD_WEAK:
        evaln.suggested_status = "weak_match"
    else:
        evaln.suggested_status = "no_match"
    return evaln


def score_product_against_listing(product: Product, cp: CompetitorProduct) -> MatchEvaluation:
    """Score one catalog product against a competitor listing (0–100)."""
    title = cp.title or ""
    t = title.lower()
    blob = _listing_text_blob(cp)

    p_ean = _norm_ean(product.ean)
    c_ean = _norm_ean(cp.ean)
    if p_ean and c_ean:
        if p_ean == c_ean or p_ean.endswith(c_ean) or c_ean.endswith(p_ean):
            # Barcode identity is authoritative: always a 100% auto match.
            evaln = MatchEvaluation(
                Decimal("100"),
                "ean_exact",
                reasons=["EAN exact match"],
                suggested_status="auto_match",
            )
            _apply_attribute_adjustments(evaln, product, cp)
            evaln.score = Decimal("100")
            evaln.suggested_status = "auto_match"
            return evaln

    p_mcode = _norm_code(product.manufacturer_code)
    c_mcode = _norm_code(cp.manufacturer_code) or _norm_code(cp.sku)
    if p_mcode and c_mcode and p_mcode == c_mcode:
        # Only the barcode auto-matches at 100%. A manufacturer-code hit is a
        # strong signal but goes to manual review: 90, or 80 when both sides
        # carry barcodes that disagree.
        conflicting_ean = bool(p_ean and c_ean)
        evaln = MatchEvaluation(
            Decimal("80") if conflicting_ean else Decimal("90"),
            "manufacturer_code_exact",
            reasons=["Manufacturer code exact match"],
        )
        if conflicting_ean:
            evaln.warnings.append("EAN differs between catalog and listing")
        _apply_attribute_adjustments(evaln, product, cp)
        return _finalize_status(evaln)

    p_model = _norm_code(getattr(product, "model", None))
    c_model = _norm_code(cp.model)
    p_sku = _norm_code(product.sku)

    if p_sku and len(p_sku) >= 5 and (p_sku in _norm_code(blob) or p_sku in t.replace(" ", "")):
        evaln = MatchEvaluation(Decimal("92"), "sku_exact", reasons=["SKU found in listing title/specs"])
        _apply_attribute_adjustments(evaln, product, cp)
        return _finalize_status(evaln)

    if p_model and c_model and p_model == c_model:
        evaln = MatchEvaluation(Decimal("91"), "model_exact", reasons=["Model exact match"])
        _apply_attribute_adjustments(evaln, product, cp)
        return _finalize_status(evaln)

    if p_model and len(p_model) >= 5 and p_model in _norm_code(blob):
        evaln = MatchEvaluation(Decimal("90"), "model_in_specs", reasons=["Model appears in listing specs"])
        _apply_attribute_adjustments(evaln, product, cp)
        return _finalize_status(evaln)

    p_brand = _norm(product.brand)
    c_brand = _norm(cp.brand)
    # Short/placeholder "brands" ("-", "x") must never count as brand-in-title.
    brand_match = bool(p_brand and c_brand and p_brand == c_brand) or bool(
        p_brand and len(p_brand) >= 3 and p_brand in t
    )
    # Known and different brands: fuzzy title tiers are meaningless (licensed
    # names like "Minnie Mouse" overlap across brands).
    brands_conflict = bool(
        p_brand and c_brand and p_brand != c_brand and p_brand not in c_brand and c_brand not in p_brand
    )

    if brand_match and p_mcode and len(p_mcode) >= 4 and (p_mcode in _norm_code(blob) or p_mcode in t.replace(" ", "")):
        evaln = MatchEvaluation(
            Decimal("90"),
            "brand_and_manufacturer_code",
            reasons=["Brand matches", "Manufacturer code appears in listing"],
        )
        _apply_attribute_adjustments(evaln, product, cp)
        return _finalize_status(evaln)

    if brand_match and p_model and len(p_model) >= 4 and p_model in _norm_code(blob):
        evaln = MatchEvaluation(
            Decimal("88"),
            "brand_and_model",
            reasons=["Brand matches", "Model appears in listing"],
        )
        _apply_attribute_adjustments(evaln, product, cp)
        return _finalize_status(evaln)

    if brands_conflict:
        return _finalize_status(
            MatchEvaluation(Decimal("0"), "brand_conflict", warnings=["Brands differ"]),
        )

    # Weak name-only tiers require brand compatibility: the listing's brand
    # must appear somewhere on the product side (or be unknown). "MINIX
    # figure" must not fuzzy-match a LEGO set that shares the license name.
    p_name_norm = _norm(product.name)
    if c_brand and not brand_match and c_brand not in p_name_norm:
        return _finalize_status(
            MatchEvaluation(
                Decimal("0"),
                "brand_missing_on_product",
                warnings=["Listing brand not found in catalog product"],
            ),
        )

    # Different declared product types ("пъзел" vs "часовник") kill the match.
    p_types = _type_tokens(product.name)
    c_types = _type_tokens(title)
    if p_types and c_types and not (p_types & c_types):
        return _finalize_status(
            MatchEvaluation(
                Decimal("0"),
                "type_conflict",
                warnings=[f"Product types differ: {', '.join(sorted(c_types))} vs {', '.join(sorted(p_types))}"],
            ),
        )

    # Fuzzy name tiers: the score IS the similarity (small brand bonus, hard
    # cap below auto). No floor — 29% similarity must score 29, not 68.
    ratio = SequenceMatcher(a=_norm(product.name), b=_norm(title)).ratio()
    if brand_match and ratio >= 0.45:
        score = Decimal(str(min(88, round(ratio * 100) + 10)))
        evaln = MatchEvaluation(
            score,
            "brand_and_fuzzy_name",
            reasons=["Brand matches", f"Title similarity {int(ratio * 100)}%"],
        )
        if not c_ean:
            evaln.warnings.append("No EAN available")
        _apply_attribute_adjustments(evaln, product, cp)
        return _finalize_status(evaln)

    if ratio >= 0.55:
        score = Decimal(str(min(79, round(ratio * 100))))
        evaln = MatchEvaluation(
            score,
            "title_similarity",
            reasons=[f"Title similarity {int(ratio * 100)}%"],
            warnings=[] if c_ean else ["No EAN available"],
        )
        _apply_attribute_adjustments(evaln, product, cp)
        return _finalize_status(evaln)

    name_tokens = set(re.findall(r"[a-z0-9]+", _norm(product.name)))
    title_tokens = set(re.findall(r"[a-z0-9]+", t))
    if name_tokens and title_tokens:
        inter = len(name_tokens & title_tokens)
        union = len(name_tokens | title_tokens)
        j = inter / union if union else 0
        if j > 0.25:
            score = Decimal(str(max(40, min(59, round(40 + j * 19)))))
            evaln = MatchEvaluation(
                score,
                "token_overlap",
                reasons=[f"Token overlap {int(j * 100)}%"],
                warnings=["No EAN available"] if not c_ean else [],
            )
            _apply_attribute_adjustments(evaln, product, cp)
            return _finalize_status(evaln)

    evaln = MatchEvaluation(
        Decimal("0"),
        "no_signal",
        warnings=["No EAN available"] if not p_ean and not c_ean else [],
    )
    return _finalize_status(evaln)


def rank_products_for_listing(
    products: list[Product],
    cp: CompetitorProduct,
    *,
    limit: int = 5,
    min_score: Decimal = Decimal("5"),
) -> list[tuple[Product, MatchEvaluation]]:
    ranked: list[tuple[Product, MatchEvaluation]] = []
    for p in products:
        evaln = score_product_against_listing(p, cp)
        if evaln.score >= min_score:
            ranked.append((p, evaln))
    ranked.sort(key=lambda x: x[1].score, reverse=True)
    return ranked[:limit]
