"""Classify listing match outcomes and serialize candidate metadata."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.models import Product
from app.services.matching import THRESHOLD_AUTO, MatchEvaluation

THRESHOLD_REVIEW = Decimal("60")
CLOSE_SCORE_GAP = Decimal("5")
WEAK_SIGNAL_MIN = Decimal("1")

SKIP_TO_REASON: dict[str, str] = {
    "confirmed": "already_confirmed",
    "rejected": "already_rejected",
    "already_matched": "already_matched",
}


@dataclass(frozen=True)
class MatchPersistPlan:
    """How batch matching should persist one listing."""

    outcome: str
    product: Product | None
    score: Decimal
    method: str
    status: str
    matched_by: str
    match_reason: str
    match_warnings: list[str]
    candidate_count: int
    top_candidates: list[dict[str, Any]]
    persist: bool = True


def skip_reason_key(skip: str) -> str:
    return SKIP_TO_REASON.get(skip, skip)


def candidate_to_dict(product: Product, evaln: MatchEvaluation) -> dict[str, Any]:
    return {
        "product_id": str(product.id),
        "sku": product.sku,
        "name": product.name,
        "brand": product.brand,
        "ean": product.ean,
        "manufacturer_code": product.manufacturer_code,
        "model": getattr(product, "model", None),
        "image_url": getattr(product, "image_url", None),
        "own_price": str(product.own_price) if product.own_price is not None else None,
        "match_score": str(evaln.score),
        "match_method": evaln.method,
        "match_reasons": list(evaln.reasons),
        "match_warnings": list(evaln.warnings),
        "suggested_status": evaln.suggested_status,
    }


def _matched_by_from_eval(evaln: MatchEvaluation, *, override: str | None = None) -> str:
    if override:
        return override
    method = (evaln.method or "").strip()
    allowed = {
        "ean_exact",
        "manufacturer_code_exact",
        "model_exact",
        "sku_exact",
        "model_in_specs",
        "brand_and_manufacturer_code",
        "brand_and_model",
        "brand_and_fuzzy_name",
        "title_similarity",
        "token_overlap",
        "no_signal",
    }
    return method if method in allowed else method or "unknown"


def _reason_text(*, status: str, matched_by: str, evaln: MatchEvaluation, close_count: int) -> str:
    if status == "needs_review" and matched_by == "multiple_candidates":
        return f"Needs review — {close_count} close candidates (top score {evaln.score})"
    if status == "low_confidence":
        return f"Low confidence match (score {evaln.score})"
    if status == "auto_matched":
        return f"Auto matched ({matched_by}, score {evaln.score})"
    if status == "needs_review":
        return f"Needs review ({matched_by}, score {evaln.score})"
    if status == "no_candidate":
        return "No candidate found"
    if evaln.reasons:
        return "; ".join(evaln.reasons[:3])
    return matched_by.replace("_", " ")


def classify_ranked_candidates(
    ranked: list[tuple[Product, MatchEvaluation]],
    *,
    min_score: Decimal,
) -> MatchPersistPlan:
    """Deterministic outcome from top catalog candidates (up to 5)."""
    top_candidates = [candidate_to_dict(p, e) for p, e in ranked[:5]]
    candidate_count = len(ranked)

    if not ranked:
        return MatchPersistPlan(
            outcome="no_candidate",
            product=None,
            score=Decimal("0"),
            method="no_signal",
            status="no_candidate",
            matched_by="no_candidate",
            match_reason="No candidate found",
            match_warnings=[],
            candidate_count=0,
            top_candidates=[],
            persist=False,
        )

    best_product, best_eval = ranked[0]
    best_score = best_eval.score

    close_high = [
        item
        for item in ranked
        if item[1].score >= min_score and (best_score - item[1].score) <= CLOSE_SCORE_GAP
    ]
    if len(close_high) >= 2:
        warnings = list(best_eval.warnings)
        warnings.append(f"{len(close_high)} candidates within {CLOSE_SCORE_GAP} points")
        return MatchPersistPlan(
            outcome="needs_review",
            product=best_product,
            score=best_score,
            method=best_eval.method,
            status="needs_review",
            matched_by="multiple_candidates",
            match_reason=_reason_text(
                status="needs_review",
                matched_by="multiple_candidates",
                evaln=best_eval,
                close_count=len(close_high),
            ),
            match_warnings=warnings,
            candidate_count=candidate_count,
            top_candidates=top_candidates,
        )

    if best_score >= THRESHOLD_AUTO:
        status = "auto_matched"
        matched_by = _matched_by_from_eval(best_eval)
        outcome = "auto_matched"
    elif best_score >= min_score:
        status = "needs_review"
        matched_by = _matched_by_from_eval(best_eval)
        outcome = "needs_review"
    elif best_score >= WEAK_SIGNAL_MIN:
        status = "low_confidence"
        matched_by = _matched_by_from_eval(best_eval)
        outcome = "low_confidence"
    else:
        return MatchPersistPlan(
            outcome="no_candidate",
            product=None,
            score=Decimal("0"),
            method=best_eval.method,
            status="no_candidate",
            matched_by="no_candidate",
            match_reason="No candidate found",
            match_warnings=list(best_eval.warnings),
            candidate_count=candidate_count,
            top_candidates=top_candidates,
            persist=False,
        )

    return MatchPersistPlan(
        outcome=outcome,
        product=best_product,
        score=best_score,
        method=best_eval.method,
        status=status,
        matched_by=matched_by,
        match_reason=_reason_text(
            status=status,
            matched_by=matched_by,
            evaln=best_eval,
            close_count=1,
        ),
        match_warnings=list(best_eval.warnings),
        candidate_count=candidate_count,
        top_candidates=top_candidates,
    )
