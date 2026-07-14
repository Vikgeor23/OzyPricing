"""Product REST router."""

import base64
import uuid

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ImportBatch, Product
from app.schemas.import_batch import ImportBatchRead
from app.schemas.price import ProductPricesResponse
from app.schemas.price_comparison import PriceComparisonPage, PriceComparisonSummary
from app.schemas.product import ProductCreate, ProductListPage, ProductRead, ProductUpdate
from app.schemas.product_import import ProductImportSummary
from app.services import product_service
from app.services.price_comparison_service import (
    build_price_comparison_page,
    build_price_comparison_summary,
    list_comparison_facets,
)
from app.celery_app import celery_app
from app.services.product_import import build_template_workbook_bytes
from app.tasks.import_tasks import import_catalog_xlsx

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/template-xlsx")
def download_product_import_template() -> Response:
    """Download empty XLSX with required column headers."""
    data = build_template_workbook_bytes()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="products_import_template.xlsx"',
        },
    )


@router.post("/import-xlsx", status_code=status.HTTP_202_ACCEPTED)
async def import_products_xlsx(file: UploadFile = File(...)) -> dict:
    """Queue an async catalog import; poll /products/import-tasks/{id} for progress."""
    fname = (file.filename or "").lower()
    if not fname.endswith(".xlsx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be a .xlsx spreadsheet",
        )
    body = await file.read()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")
    async_result = import_catalog_xlsx.delay(
        base64.b64encode(body).decode(),
        file.filename or "catalog.xlsx",
    )
    return {"task_id": str(async_result.id)}


@router.get("/import-tasks/{task_id}")
def get_import_task_status(task_id: str) -> dict:
    """Poll async import progress and final summary."""
    async_result = AsyncResult(task_id, app=celery_app)
    meta = async_result.info if isinstance(async_result.info, dict) else {}
    payload: dict = {
        "task_id": task_id,
        "state": async_result.state,
        "ready": async_result.ready(),
        "current": int(meta.get("current", 0) or 0),
        "total": int(meta.get("total", 0) or 0),
        "phase": meta.get("phase"),
    }
    if async_result.successful() and isinstance(async_result.result, dict):
        payload["result"] = async_result.result
    elif async_result.failed():
        payload["error"] = str(async_result.result)
    return payload


@router.get("/imports", response_model=list[ImportBatchRead])
def list_import_batches(db: Session = Depends(get_db)) -> list[ImportBatchRead]:
    """All catalog uploads, newest first, with live product counts."""
    product_counts = dict(
        db.execute(
            select(Product.import_batch_id, func.count())
            .where(Product.import_batch_id.isnot(None))
            .group_by(Product.import_batch_id),
        ).all(),
    )
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc())).all()
    return [
        ImportBatchRead(
            id=b.id,
            filename=b.filename,
            created_at=b.created_at,
            total_rows=b.total_rows,
            imported_rows=b.imported_rows,
            skipped_rows=b.skipped_rows,
            product_count=int(product_counts.get(b.id, 0)),
        )
        for b in batches
    ]


@router.delete("/imports/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_import_batch(batch_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    """Delete an upload and every product that came from it (cascade)."""
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    db.delete(batch)
    db.commit()


@router.get("/imports/{batch_id}/products", response_model=ProductListPage)
def list_import_batch_products(
    batch_id: uuid.UUID,
    limit: int = Query(default=75, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ProductListPage:
    if db.get(ImportBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    total = int(
        db.scalar(select(func.count()).where(Product.import_batch_id == batch_id)) or 0,
    )
    rows = db.scalars(
        select(Product)
        .where(Product.import_batch_id == batch_id)
        .order_by(Product.created_at.desc())
        .limit(limit)
        .offset(offset),
    ).all()
    return ProductListPage(
        rows=[ProductRead.model_validate(p) for p in rows],
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(rows)) < total,
    )


@router.get("/price-comparison", response_model=PriceComparisonPage)
def get_price_comparison(
    limit: int = Query(default=75, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=255),
    only_matched: bool = Query(default=False),
    category: str | None = Query(default=None, max_length=255),
    brand: str | None = Query(default=None, max_length=255),
    competitor_id: uuid.UUID | None = Query(default=None),
    hide_out_of_stock: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> PriceComparisonPage:
    """Paginated product rows with competitor prices and status flags.

    ``only_matched=true`` keeps only products with a confirmed/auto match (the
    comparison-matrix view) and returns the competitor column list.
    """
    return build_price_comparison_page(
        db,
        limit=limit,
        offset=offset,
        search=search,
        only_matched=only_matched,
        category=category,
        brand=brand,
        competitor_id=competitor_id,
        hide_out_of_stock=hide_out_of_stock,
    )


@router.get("/price-comparison-facets")
def get_price_comparison_facets(db: Session = Depends(get_db)) -> dict[str, list[str]]:
    """Distinct category/brand values across matched products (filter options)."""
    return list_comparison_facets(db)


@router.get("/price-comparison-summary", response_model=PriceComparisonSummary)
def get_price_comparison_summary(db: Session = Depends(get_db)) -> PriceComparisonSummary:
    """Compact global totals for the price comparison header."""
    return build_price_comparison_summary(db)


@router.get("", response_model=ProductListPage)
def list_products(
    limit: int = Query(default=75, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ProductListPage:
    return product_service.list_products_page(db, limit=limit, offset=offset)


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, db: Session = Depends(get_db)) -> ProductRead:
    product = product_service.create_product(db, payload)
    return ProductRead.model_validate(product)


@router.get("/{product_id}", response_model=ProductRead)
def get_product(product_id: uuid.UUID, db: Session = Depends(get_db)) -> ProductRead:
    product = product_service.get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return ProductRead.model_validate(product)


@router.put("/{product_id}", response_model=ProductRead)
def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
) -> ProductRead:
    product = product_service.get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    product = product_service.update_product(db, product, payload)
    return ProductRead.model_validate(product)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(product_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    product = product_service.get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    product_service.delete_product(db, product)


@router.get("/{product_id}/prices", response_model=ProductPricesResponse)
def get_product_prices(product_id: uuid.UUID, db: Session = Depends(get_db)) -> ProductPricesResponse:
    result = product_service.get_product_prices(db, product_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return result
