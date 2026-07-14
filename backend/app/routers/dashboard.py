"""Dashboard summary for the SPA."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.dashboard_service import DashboardProductsPage, build_dashboard_page

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/products", response_model=DashboardProductsPage)
def dashboard_products(
    limit: int = Query(default=75, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> DashboardProductsPage:
    return build_dashboard_page(db, limit=limit, offset=offset)
