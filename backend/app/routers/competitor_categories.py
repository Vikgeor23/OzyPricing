"""Competitor category workspace endpoints + batch Celery jobs."""



import uuid



from fastapi import APIRouter, Depends, HTTPException, Query, status

from sqlalchemy.orm import Session



from app.database import get_db

from app.schemas.workspace_page import CategoryWorkspacePage

from app.schemas.competitor_tree import DiscoverQueued

from app.services import scrape_run_lock
from app.services.competitor_category_service import get_category

from app.services.workspace_query import WorkspaceQueryParams, list_category_workspace_page

from app.tasks.discovery_tasks import (

    discover_products_category,

    find_matches_category,

    scrape_prices_category,

)



router = APIRouter(prefix="/competitor-categories", tags=["competitor-categories"])





@router.get("/{category_id}/products", response_model=CategoryWorkspacePage)

def get_category_products(

    category_id: uuid.UUID,

    limit: int = Query(default=75, ge=1, le=100),

    offset: int = Query(default=0, ge=0),

    search: str | None = Query(default=None),

    status: str | None = Query(default=None),

    has_price: bool | None = Query(default=None),

    scraped: bool | None = Query(default=None),

    sort_by: str = Query(default="last_scraped_at"),

    sort_dir: str = Query(default="desc"),

    db: Session = Depends(get_db),

) -> CategoryWorkspacePage:

    page = list_category_workspace_page(

        db,

        category_id,

        WorkspaceQueryParams(

            limit=limit,

            offset=offset,

            search=search,

            status=status,

            has_price=has_price,

            scraped=scraped,

            sort_by=sort_by,

            sort_dir=sort_dir,

        ),

    )

    if page is None:

        raise HTTPException(status_code=404, detail="Category not found")

    return page





@router.post(

    "/{category_id}/discover-products",

    status_code=status.HTTP_202_ACCEPTED,

    response_model=DiscoverQueued,

)

def enqueue_discover_products(category_id: uuid.UUID, db: Session = Depends(get_db)) -> DiscoverQueued:

    if get_category(db, category_id) is None:

        raise HTTPException(status_code=404, detail="Category not found")



    async_result = discover_products_category.delay(str(category_id))

    scrape_run_lock.mark_queued(str(async_result.id))

    return DiscoverQueued(task_id=str(async_result.id))





@router.post(

    "/{category_id}/scrape-prices",

    status_code=status.HTTP_202_ACCEPTED,

    response_model=DiscoverQueued,

)

def enqueue_scrape_prices(category_id: uuid.UUID, db: Session = Depends(get_db)) -> DiscoverQueued:

    if get_category(db, category_id) is None:

        raise HTTPException(status_code=404, detail="Category not found")



    async_result = scrape_prices_category.delay(str(category_id))

    scrape_run_lock.mark_queued(str(async_result.id))

    return DiscoverQueued(task_id=str(async_result.id))





@router.post(

    "/{category_id}/find-matches",

    status_code=status.HTTP_202_ACCEPTED,

    response_model=DiscoverQueued,

)

def enqueue_find_matches(category_id: uuid.UUID, db: Session = Depends(get_db)) -> DiscoverQueued:

    if get_category(db, category_id) is None:

        raise HTTPException(status_code=404, detail="Category not found")



    async_result = find_matches_category.delay(str(category_id))

    scrape_run_lock.mark_queued(str(async_result.id))

    return DiscoverQueued(task_id=str(async_result.id))

