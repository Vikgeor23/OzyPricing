"""Product match confirm/reject API."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ProductMatch
from app.schemas.match import MatchConfirmBody, MatchRejectBody
from app.services import match_service

router = APIRouter(prefix="/matches", tags=["matches"])


@router.post("/confirm", response_model=dict)
def confirm_match(
    body: MatchConfirmBody,
    db: Session = Depends(get_db),
) -> dict:
    try:
        row: ProductMatch = match_service.upsert_match_and_link_product(db, body)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"status": "ok", "match_id": str(row.id)}


@router.post("/reject", response_model=dict)
def reject_match(
    body: MatchRejectBody,
    db: Session = Depends(get_db),
) -> dict:
    try:
        row: ProductMatch = match_service.reject_match(db, body)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"status": "ok", "match_id": str(row.id)}
