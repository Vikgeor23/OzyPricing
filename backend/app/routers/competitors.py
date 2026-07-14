"""Competitor REST router."""

import time
import uuid

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.services.auth_service import get_user_by_token
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import get_settings
from app.database import get_db
from app.models import Competitor, CompetitorProduct, ProductMatch
from app.schemas.competitor import CompetitorCreate, CompetitorRead, CompetitorUpdate
from app.schemas.competitor_stats import CompetitorStats, DiscoverySourceCount
from app.utils.url_utils import is_technopolis, normalize_domain
from app.schemas.competitor_tree import CompetitorTreeItem, DiscoverQueued, DiscoveryTaskStatus
from app.schemas.discovery_batch import DiscoverAllBody, DiscoverAllQueued
from app.schemas.match_batch import MatchAllBody, MatchAllQueued, MatchTaskStatus, match_task_status_from_meta
from app.schemas.scrape_batch import ScrapeAllBody, ScrapeAllQueued, ScrapeTaskStatus, scrape_task_status_from_meta
from app.schemas.workspace_page import CategoryWorkspacePage
from app.services import competitor_service
from app.services import scrape_run_lock
from app.services.scrape_cancel import request_cancel as request_scrape_cancel
from app.services.workspace_query import WorkspaceQueryParams, list_competitor_workspace_page
from app.services.workspace_export import build_workspace_export_xlsx
from app.services.competitor_tree_service import build_competitor_forest
from app.tasks.discovery_tasks import (
    discover_all_product_urls_for_competitor,
    discover_categories_competitor,
    probe_competitor_site,
)
from app.tasks.match_tasks import match_competitor_products_batch
from app.tasks.scrape_batch_tasks import scrape_competitor_products_batch

router = APIRouter(prefix="/competitors", tags=["competitors"])


@router.get("/{competitor_id}/stats", response_model=CompetitorStats)
def competitor_stats(competitor_id: uuid.UUID, db: Session = Depends(get_db)) -> CompetitorStats:
    """Aggregated dashboard metrics for one competitor."""
    competitor = competitor_service.get_competitor(db, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")

    # Listing counters: one pass over this competitor's rows, no joins — the
    # old single query outer-joined a window over ALL product_matches into the
    # 3.1M-row scan and could run for minutes on big competitors.
    row = db.execute(
        select(
            func.count().label("total"),
            func.count().filter(CompetitorProduct.latest_scrape_status == "scraped").label("scraped"),
            func.count().filter(CompetitorProduct.latest_price.isnot(None)).label("with_price"),
            func.count().filter(CompetitorProduct.latest_scrape_status == "failed").label("failed"),
            func.count().filter(CompetitorProduct.latest_scrape_status.is_(None)).label("never"),
            func.count().filter(CompetitorProduct.is_dead.is_(True)).label("dead"),
            func.count().filter(CompetitorProduct.product_id.isnot(None)).label("matched"),
            func.max(CompetitorProduct.latest_scraped_at).label("last_scraped_at"),
            func.max(func.coalesce(CompetitorProduct.discovered_at, CompetitorProduct.created_at)).label(
                "last_discovered_at",
            ),
        ).where(CompetitorProduct.competitor_id == competitor_id),
    ).one()

    # Match-status counters: best match per listing, windowed over only this
    # competitor's matches (thousands of rows, not the whole table).
    status_rank = case(
        (ProductMatch.status == "confirmed", 0),
        (ProductMatch.status == "auto_matched", 1),
        (ProductMatch.status == "needs_review", 2),
        (ProductMatch.status == "low_confidence", 3),
        else_=4,
    )
    ranked = (
        select(
            ProductMatch.status,
            func.row_number()
            .over(
                partition_by=ProductMatch.competitor_product_id,
                order_by=(status_rank.asc(), ProductMatch.match_score.desc()),
            )
            .label("rn"),
        )
        .join(CompetitorProduct, CompetitorProduct.id == ProductMatch.competitor_product_id)
        .where(
            CompetitorProduct.competitor_id == competitor_id,
            CompetitorProduct.product_id.is_(None),
            ProductMatch.status != "rejected",
        )
        .subquery("pm_ranked")
    )
    match_counts = {
        status: int(count)
        for status, count in db.execute(
            select(ranked.c.status, func.count()).where(ranked.c.rn == 1).group_by(ranked.c.status),
        ).all()
    }

    sources = db.execute(
        select(
            func.coalesce(CompetitorProduct.discovery_source, "manual").label("source"),
            func.count().label("count"),
        )
        .where(CompetitorProduct.competitor_id == competitor_id)
        .group_by("source")
        .order_by(func.count().desc()),
    ).all()

    settings = get_settings()
    domain = normalize_domain(competitor.domain or "").removeprefix("www.")
    magento_http_domains = {
        d.strip().removeprefix("www.")
        for d in (settings.scrape_magento_bulk_domains or "").split(",")
        if d.strip()
    }
    if is_technopolis(domain):
        scrape_method = "Technopolis adapter (OCC API + browser pool)"
    elif "douglas" in domain and settings.scrape_douglas_bulk_enabled:
        scrape_method = "Magento catalog feed (browser session)"
    elif domain in magento_http_domains:
        scrape_method = "Magento catalog feed (direct API)"
    elif any(s.source == "magento_graphql_bulk" for s in sources) or _magento_bulk_marker(domain):
        # Batch scrape autodetected a Magento /graphql feed on a previous run.
        scrape_method = "Magento catalog feed (autodetected)"
    else:
        scrape_method = "Per-page HTTP with browser fallback"

    total = int(row.total or 0)
    return CompetitorStats(
        competitor_id=str(competitor_id),
        total_urls=total,
        scraped=int(row.scraped or 0),
        with_price=int(row.with_price or 0),
        failed=int(row.failed or 0),
        never_scraped=int(row.never or 0),
        dead_urls=int(row.dead or 0),
        matched=int(row.matched or 0),
        auto_matched=match_counts.get("auto_matched", 0),
        needs_review=match_counts.get("needs_review", 0),
        low_confidence=match_counts.get("low_confidence", 0),
        coverage_pct=round(100.0 * int(row.with_price or 0) / total, 1) if total else 0.0,
        last_scraped_at=row.last_scraped_at,
        last_discovered_at=row.last_discovered_at,
        discovery_sources=[DiscoverySourceCount(source=s.source, count=int(s.count)) for s in sources],
        scrape_method=scrape_method,
    )


def _magento_bulk_marker(domain: str) -> bool:
    """Check the Redis marker written when batch scrape autodetects a Magento
    /graphql bulk feed for this domain (cheap; no live probing here)."""
    try:
        import redis

        client = redis.Redis.from_url(get_settings().redis_url, socket_timeout=1)
        return client.get(f"pm:scrape:magento_bulk:{domain}") is not None
    except Exception:  # noqa: BLE001
        return False


@router.get("/tree", response_model=list[CompetitorTreeItem])
def competitor_tree(db: Session = Depends(get_db)) -> list[CompetitorTreeItem]:
    """Competitors with nested ``CompetitorCategory`` rows for the explorer UI."""

    return build_competitor_forest(db)


def _progress_is_stale(state: str, meta: dict) -> bool:
    """A PROGRESS meta stops updating when the worker dies (restart/OOM) —
    Celery never finalizes it, so treat a silent heartbeat as a dead task.
    Metas written before heartbeat_at existed count as stale too."""
    if state != "PROGRESS":
        return False
    heartbeat = meta.get("heartbeat_at")
    if not isinstance(heartbeat, (int, float)):
        return True
    return (time.time() - heartbeat) > get_settings().scrape_progress_stale_sec


_STALE_PROGRESS_ERROR = "progress_stalled: worker restarted or crashed mid-run; start a new run"

_LOST_TASK_ERROR = "task_lost: run no longer exists (server restarted or result expired); start a new run"


def _pending_is_dead(task_id: str) -> bool:
    """PENDING is Celery's answer both for a queued task and for an id it has
    never heard of (result expired or lost in a restart). Queued messages sit
    in the broker, and real runs write a PROGRESS meta within seconds of
    pickup — so a PENDING task that is neither in the broker nor recently
    enqueued is dead and must not spin in the UI forever."""
    if scrape_run_lock.waiting_in_broker(task_id):
        return False
    queued = scrape_run_lock.queued_at(task_id)
    if queued is None:
        return True
    return (time.time() - queued) > get_settings().scrape_progress_stale_sec


def _finalize_dead_pending(meta: dict) -> tuple[str, bool, dict]:
    return "FAILURE", True, {**meta, "errors": [*(meta.get("errors") or []), _LOST_TASK_ERROR]}


@router.get("/discovery-tasks/{task_id}", response_model=DiscoveryTaskStatus)
def get_discovery_task_status(task_id: str) -> DiscoveryTaskStatus:
    """Poll Celery discovery task state/result (full-domain or category harvest)."""
    async_result = AsyncResult(task_id, app=celery_app)
    meta = async_result.info if isinstance(async_result.info, dict) else {}
    payload: dict | None = None
    state = str(async_result.state)
    ready = async_result.ready()
    if async_result.ready():
        raw = async_result.result
        payload = raw if isinstance(raw, dict) else {"value": raw}
        if isinstance(payload, dict):
            meta = {**meta, **payload}
    elif _progress_is_stale(state, meta):
        state = "FAILURE"
        ready = True
        meta = {**meta, "errors": [*(meta.get("errors") or []), _STALE_PROGRESS_ERROR]}
    elif state == "PENDING" and _pending_is_dead(task_id):
        state, ready, meta = _finalize_dead_pending(meta)

    return DiscoveryTaskStatus(
        task_id=task_id,
        state=state,
        ready=ready,
        current_phase=meta.get("current_phase"),
        current=int(meta.get("current", 0) or 0),
        total=int(meta.get("total", 0) or 0),
        product_urls_found=int(meta.get("product_urls_found", 0) or 0),
        new_urls_found=int(meta.get("new_urls_found", 0) or 0),
        created=int(meta.get("created", 0) or 0),
        skipped_existing=int(meta.get("skipped_existing", 0) or 0),
        categories_updated=int(meta.get("categories_updated", 0) or 0),
        sitemap_files_checked=int(meta.get("sitemap_files_checked", 0) or 0),
        pages_scanned=int(meta.get("pages_scanned", 0) or 0),
        external_queries_checked=int(meta.get("external_queries_checked", 0) or 0),
        rate_limit_pauses=int(meta.get("rate_limit_pauses", 0) or 0),
        duration_ms=meta.get("duration_ms"),
        errors=list(meta.get("errors") or []),
        sample_new_urls=list(meta.get("sample_new_urls") or []),
        sample_existing_urls=list(meta.get("sample_existing_urls") or []),
        discovery_methods=list(meta.get("discovery_methods") or []),
        probe=meta.get("probe") if isinstance(meta.get("probe"), dict) else None,
        result=payload,
    )


@router.post("/scrape-tasks/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_scrape_task(task_id: str) -> dict[str, str]:
    """Ask a running batch scrape to stop after the current chunk (cooperative)."""
    request_scrape_cancel(task_id)
    return {"status": "cancel_requested", "task_id": task_id}


@router.post("/discovery-tasks/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_discovery_task(task_id: str) -> dict[str, str]:
    """Ask a running URL discovery to stop; URLs found so far are still saved."""
    request_scrape_cancel(task_id)
    return {"status": "cancel_requested", "task_id": task_id}


@router.get("/scrape-tasks/{task_id}", response_model=ScrapeTaskStatus)
def get_scrape_task_status(task_id: str) -> ScrapeTaskStatus:
    """Poll Celery batch scrape task state and progress."""
    async_result = AsyncResult(task_id, app=celery_app)
    meta = async_result.info if isinstance(async_result.info, dict) else {}
    state = str(async_result.state)
    ready = async_result.ready()
    if state == "REVOKED":
        # Stopped via revoke (manual stop / worker restart): the raw result is
        # an exception object, not a progress dict — report it as a stopped
        # run instead of a silent zero-product success.
        return scrape_task_status_from_meta(
            task_id,
            "FAILURE",
            True,
            {**meta, "current_phase": "cancelled", "errors": [*(meta.get("errors") or []), "run_stopped: task was revoked (stop or worker restart)"]},
        )
    if async_result.ready():
        raw = async_result.result
        payload = raw if isinstance(raw, dict) else {"value": raw}
        if isinstance(payload, dict):
            meta = {**meta, **payload}
            meta["result"] = payload
    elif _progress_is_stale(state, meta):
        state = "FAILURE"
        ready = True
        meta = {**meta, "errors": [*(meta.get("errors") or []), _STALE_PROGRESS_ERROR]}
    elif state == "PENDING" and _pending_is_dead(task_id):
        state, ready, meta = _finalize_dead_pending(meta)

    return scrape_task_status_from_meta(task_id, state, ready, meta)


@router.get("/match-tasks/{task_id}", response_model=MatchTaskStatus)
def get_match_task_status(task_id: str) -> MatchTaskStatus:
    """Poll Celery batch match task state and progress."""
    async_result = AsyncResult(task_id, app=celery_app)
    meta = async_result.info if isinstance(async_result.info, dict) else {}
    payload: dict | None = None
    state = str(async_result.state)
    ready = async_result.ready()
    if async_result.ready():
        raw = async_result.result
        payload = raw if isinstance(raw, dict) else {"value": raw}
        if isinstance(payload, dict):
            meta = {**meta, **payload}
            meta["result"] = payload
    elif _progress_is_stale(state, meta):
        state = "FAILURE"
        ready = True
        meta = {**meta, "errors": [*(meta.get("errors") or []), _STALE_PROGRESS_ERROR]}
    elif state == "PENDING" and _pending_is_dead(task_id):
        state, ready, meta = _finalize_dead_pending(meta)

    return match_task_status_from_meta(task_id, state, ready, meta)


@router.get("", response_model=list[CompetitorRead])
def list_competitors(db: Session = Depends(get_db)) -> list[CompetitorRead]:
    return [CompetitorRead.model_validate(c) for c in competitor_service.list_competitors(db)]


@router.post("", response_model=CompetitorRead, status_code=status.HTTP_201_CREATED)
def create_competitor(payload: CompetitorCreate, db: Session = Depends(get_db)) -> CompetitorRead:
    competitor = competitor_service.create_competitor(db, payload)
    return CompetitorRead.model_validate(competitor)


@router.post(
    "/{competitor_id}/discover-categories",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DiscoverQueued,
)
def enqueue_discover_categories(competitor_id: uuid.UUID, db: Session = Depends(get_db)) -> DiscoverQueued:
    """Queue Technopolis.bg category discovery (MVP: technopolis domains only)."""

    competitor = competitor_service.get_competitor(db, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")

    async_result = discover_categories_competitor.delay(str(competitor_id))
    return DiscoverQueued(task_id=str(async_result.id))


@router.post(
    "/{competitor_id}/discovery-probe",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DiscoverQueued,
)
def enqueue_discovery_probe(competitor_id: uuid.UUID, db: Session = Depends(get_db)) -> DiscoverQueued:
    """Queue a cheap site probe that ranks discovery methods for this domain."""
    competitor = competitor_service.get_competitor(db, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")

    async_result = probe_competitor_site.delay(str(competitor_id))
    return DiscoverQueued(task_id=str(async_result.id))


@router.post(
    "/{competitor_id}/discover-all-product-urls",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DiscoverAllQueued,
)
def enqueue_discover_all_product_urls(
    competitor_id: uuid.UUID,
    request: Request,
    body: DiscoverAllBody | None = None,
    db: Session = Depends(get_db),
) -> DiscoverAllQueued:
    """Queue incremental full-domain product URL discovery (no price scraping)."""
    competitor = competitor_service.get_competitor(db, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")

    opts = body or DiscoverAllBody()
    async_result = discover_all_product_urls_for_competitor.delay(
        str(competitor_id),
        opts.only_new,
        opts.force_rescan,
        opts.limit,
        opts.source,
        opts.deep_discovery,
        opts.seed_terms,
        opts.max_search_queries,
        opts.discovery_methods,
    )
    scrape_run_lock.mark_run("discovery", str(competitor_id), str(async_result.id))
    owner = _requester_email(request, db)
    if owner:
        scrape_run_lock.mark_owner(str(async_result.id), owner)
    mode = "new URLs only" if opts.only_new and not opts.force_rescan else "full rescan"
    return DiscoverAllQueued(
        task_id=str(async_result.id),
        message=f"Product URL discovery queued ({mode}). This can take several minutes.",
    )


@router.post(
    "/{competitor_id}/match-all",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MatchAllQueued,
)
def enqueue_match_all(
    competitor_id: uuid.UUID,
    request: Request,
    body: MatchAllBody | None = None,
    db: Session = Depends(get_db),
) -> MatchAllQueued:
    """Queue batch matching for all or category-scoped competitor products."""
    if competitor_service.get_competitor(db, competitor_id) is None:
        raise HTTPException(status_code=404, detail="Competitor not found")

    opts = body or MatchAllBody()
    async_result = match_competitor_products_batch.delay(
        str(competitor_id),
        str(opts.category_id) if opts.category_id else None,
        opts.only_unmatched,
        opts.limit,
        opts.min_score,
    )
    scrape_run_lock.mark_run("match", str(competitor_id), str(async_result.id))
    owner = _requester_email(request, db)
    if owner:
        scrape_run_lock.mark_owner(str(async_result.id), owner)
    scope = "category" if opts.category_id else "competitor"
    return MatchAllQueued(
        task_id=str(async_result.id),
        message=f"Batch match queued for {scope} listings.",
    )


@router.post(
    "/{competitor_id}/scrape-all",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ScrapeAllQueued,
)
def enqueue_scrape_all(
    competitor_id: uuid.UUID,
    request: Request,
    body: ScrapeAllBody | None = None,
    db: Session = Depends(get_db),
) -> ScrapeAllQueued:
    """Queue batch scraping for all or category-scoped competitor products."""
    if competitor_service.get_competitor(db, competitor_id) is None:
        raise HTTPException(status_code=404, detail="Competitor not found")

    # A second user (or browser) launching the same competitor attaches to the
    # already-running task instead of starting a duplicate run.
    existing = scrape_run_lock.active_task(str(competitor_id))
    if existing:
        if _task_is_live(existing):
            return ScrapeAllQueued(
                task_id=existing,
                message="A batch scrape is already running for this competitor — attached to its progress.",
            )
        # Dead run (revoked / worker restarted) left its slot behind — free it
        # so it can't confuse anyone until the TTL backstop.
        scrape_run_lock.release(str(competitor_id), existing)

    opts = body or ScrapeAllBody()
    async_result = scrape_competitor_products_batch.delay(
        str(competitor_id),
        str(opts.category_id) if opts.category_id else None,
        opts.only_missing,
        opts.only_stale,
        opts.stale_hours,
        opts.limit,
        opts.skip_recent_failures,
        opts.recent_failure_hours,
    )
    scrape_run_lock.mark_active(str(competitor_id), str(async_result.id))
    owner = _requester_email(request, db)
    if owner:
        scrape_run_lock.mark_owner(str(async_result.id), owner)
    scope = "category" if opts.category_id else "competitor"
    return ScrapeAllQueued(
        task_id=str(async_result.id),
        message=f"Batch scrape queued for {scope} listings.",
    )


def _requester_email(request: Request, db: Session) -> str | None:
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not token:
        return None
    user = get_user_by_token(db, token)
    return user.email if user is not None else None


@router.get("/active-scrape-tasks")
def list_active_scrape_tasks(request: Request, db: Session = Depends(get_db)) -> list[dict[str, str]]:
    """Live batch runs (scrape / match / discovery) started by this account,
    across devices/sessions.

    Runs without a recorded owner (started via CLI/older versions) stay
    visible to everyone rather than disappearing for all.
    """
    runs: list[tuple[str, str, str]] = [
        ("scrape", cid, tid) for cid, tid in scrape_run_lock.list_active().items()
    ]
    for kind in ("match", "discovery"):
        runs.extend((kind, cid, tid) for cid, tid in scrape_run_lock.list_runs(kind).items())
    if not runs:
        return []
    requester = _requester_email(request, db)
    runs = [
        (kind, cid, tid)
        for kind, cid, tid in runs
        if (owner := scrape_run_lock.owner_of(tid)) is None or owner == requester
    ]
    if not runs:
        return []
    names = {
        str(row.id): row.name
        for row in db.execute(
            select(Competitor.id, Competitor.name).where(
                Competitor.id.in_({uuid.UUID(cid) for _, cid, _ in runs}),
            ),
        ).all()
    }
    return [
        {
            "task_id": tid,
            "competitor_id": cid,
            "competitor_name": names.get(cid, "competitor"),
            "kind": kind,
        }
        for kind, cid, tid in runs
        if _task_is_live(tid)
    ]


def _task_is_live(task_id: str) -> bool:
    """Running or queued, with a fresh heartbeat — dead tasks don't hold the slot."""
    async_result = AsyncResult(task_id, app=celery_app)
    if async_result.ready():
        return False
    state = str(async_result.state)
    if state == "PENDING" and _pending_is_dead(task_id):
        return False
    meta = async_result.info if isinstance(async_result.info, dict) else {}
    return not _progress_is_stale(state, meta)


@router.get("/{competitor_id}/products", response_model=CategoryWorkspacePage)
def list_competitor_products_workspace(
    competitor_id: uuid.UUID,
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
    """Paginated competitor listings for workspace view (includes uncategorized)."""
    page = list_competitor_workspace_page(
        db,
        competitor_id,
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
        raise HTTPException(status_code=404, detail="Competitor not found")
    return page


@router.get("/{competitor_id}/products/export-xlsx")
def export_competitor_products_workspace(
    competitor_id: uuid.UUID,
    category_id: uuid.UUID | None = Query(default=None),
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    has_price: bool | None = Query(default=None),
    scraped: bool | None = Query(default=None),
    sort_by: str = Query(default="last_scraped_at"),
    sort_dir: str = Query(default="desc"),
    db: Session = Depends(get_db),
) -> Response:
    """Export filtered competitor workspace rows to Excel."""
    data = build_workspace_export_xlsx(
        db,
        competitor_id=competitor_id,
        category_id=category_id,
        params=WorkspaceQueryParams(
            limit=100,
            offset=0,
            search=search,
            status=status,
            has_price=has_price,
            scraped=scraped,
            sort_by=sort_by,
            sort_dir=sort_dir,
        ),
    )
    if data is None:
        raise HTTPException(status_code=404, detail="Competitor or category not found")
    filename = f"competitor_{competitor_id}_products.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{competitor_id}", response_model=CompetitorRead)
def get_competitor(competitor_id: uuid.UUID, db: Session = Depends(get_db)) -> CompetitorRead:
    competitor = competitor_service.get_competitor(db, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")
    return CompetitorRead.model_validate(competitor)


@router.put("/{competitor_id}", response_model=CompetitorRead)
def update_competitor(
    competitor_id: uuid.UUID,
    payload: CompetitorUpdate,
    db: Session = Depends(get_db),
) -> CompetitorRead:
    competitor = competitor_service.get_competitor(db, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")
    competitor = competitor_service.update_competitor(db, competitor, payload)
    return CompetitorRead.model_validate(competitor)


@router.delete("/{competitor_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_competitor(competitor_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    competitor = competitor_service.get_competitor(db, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")
    competitor_service.delete_competitor(db, competitor)
