# Future Production Architecture (10M+ Competitor Listings)

**Project:** Pricing Monitor  
**Document type:** Read-only architecture design (no code changes)  
**Scale target:** **10M+** rows in `competitor_products` (possibly multiple competitors), thousands of scrape jobs in flight, hundreds of concurrent UI sessions, Celery workers on multiple Linux nodes.

**Ground rule:** Every recommendation maps from **today’s code** (`backend/app/*`, `frontend/app/competitors/page.tsx`, `docker-compose.yml`) to a **concrete evolution path**. This is not a generic “microservices on Kubernetes” template.

**Companion audits:**

- `docs/audits/app_architecture_onboarding_audit.md` — current behavior
- `docs/audits/scalability_and_realtime_audit.md` — breaking points and bottlenecks

---

## 1. Executive summary

### What exists today (the baseline)

| Concern | Current implementation |
|---------|------------------------|
| API | Sync FastAPI `create_app()` in `main.py`; routers under `/competitors`, `/competitor-products`, etc.; mirrored at `/api/*` |
| Long work | Celery `celery_app` with **one default queue**; tasks in `tasks/scrape_tasks.py`, `scrape_batch_tasks.py`, `discovery_tasks.py`, `match_tasks.py` |
| Scrape | `run_batch_scrape_competitor_products()` in `scraping_batch.py` (one mega-task, `asyncio.run`, `TechnopolisPlaywrightPool`, `AdaptiveConcurrencyController`) |
| Match | `run_batch_match_competitor_products()` in `matching_batch.py` (per-listing scoring via `matching.py` + `matching_catalog.py`) |
| Progress | Celery `update_state(PROGRESS, meta=…)` → poll `GET /competitors/{scrape\|match\|discovery}-tasks/{task_id}` (`routers/competitors.py`) |
| UI | `CompetitorsPage` — 2s poll + 5s workspace refresh (`frontend/app/competitors/page.tsx`) |
| Reads | `paginate_workspace()` + `best_match_subquery()` (`workspace_query.py`, `db/latest_price.py`) |
| Storage | Postgres for all state; Redis for Celery only; screenshots on disk `backend/storage/scrape_failures/` (`technopolis.py` `FAILURE_DIR`) |

### What 10M+ requires (the target)

The **same product semantics** (own catalog import, competitor tree, workspace table, match confirm/reject, Technopolis scrape stack) must run on an **orchestrated, partitioned, event-emitting** platform:

1. **Shard work** — replace monolithic batch Celery tasks with **many small, idempotent units** (one listing scrape, one listing match) coordinated by a **job record** in Postgres.
2. **Partition data** — `competitor_products` (and optionally `product_matches`) partitioned or keyed by `competitor_id`; eliminate `OFFSET` on million-row scans.
3. **Split workers** — dedicated fleets: OCC-fast scrape, Playwright scrape, CPU match, I/O discovery — routed via **named Celery queues** (extension of `celery_app.py`).
4. **Realtime** — keep existing `ScrapeTaskStatus` / `MatchTaskStatus` / `DiscoveryTaskStatus` shapes; deliver via **SSE** (and optional WebSocket) fed from Redis pub/sub, not 2s `AsyncResult` polling.
5. **Event log** — append-only `job_events` (and optional outbox) so UI, analytics, and retries share one timeline.

### Phased migration (high level)

| Phase | Scale | Core change |
|-------|-------|-------------|
| **A** (harden MVP) | 10⁵ listings | Queues, locks, kill `scrape_prices_category` fan-out, pool for all scrapes, bulk match skip |
| **B** (orchestrated) | 10⁶ listings | `jobs` + `job_chunks` tables; chunk tasks; keyset workspace; denormalized `workspace_status` |
| **C** (10M+) | 10⁷ listings | Partitioned tables, read replicas, OCC-only fleet at scale, match inverted index, SSE default |

---

## 2. Target architecture (mapped to this repo)

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  Next.js  CompetitorsPage / ProductsPage / IntegrationsPage                 │
│  lib/api.ts  lib/types.ts (ScrapeTaskStatus, MatchTaskStatus, …)            │
│  NEW: EventSource → GET /api/jobs/{job_id}/events  (SSE, same JSON shape)   │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────────────────┐
│  FastAPI  app/main.py                                                         │
│  EXISTING: routers/competitors.py (enqueue + poll)                            │
│  NEW:      routers/jobs.py (job CRUD, SSE stream, cancel)                     │
│  READ PATH: workspace_query.py → replica + materialized status                │
│  WRITE PATH: async 202 only — no full-catalog find-matches sync               │
└───────┬───────────────────────────────┬─────────────────────────────────────┘
        │                               │
        │ SQL (PgBouncer)                │ Redis
        ▼                               ▼
┌───────────────────┐           ┌─────────────────────────────────────────────┐
│ PostgreSQL 16+    │           │ Redis Cluster (logical roles)              │
│ • competitor_     │           │ • DB0: Celery broker (lists)                 │
│   products (PART) │           │ • DB1: Celery results (short TTL)            │
│ • jobs / chunks   │           │ • DB2: pub/sub job:{id}:events               │
│ • products        │           │ • DB3: locks scrape:{cp_id}, match:{cp_id}   │
│ • product_matches │           └─────────────────────────────────────────────┘
│ • job_events      │                         │
└───────────────────┘                         │
        ▲                                     │
        │         ┌───────────────────────────┴───────────────────────────┐
        │         │ Celery workers (horizontal, queue-specific)            │
        │         │ include= tasks/scrape_tasks, scrape_batch_tasks, …       │
        │         │                                                             │
        │         │  scrape_occ_queue    → scrape_listing_occ (thin wrapper)   │
        │         │  scrape_pw_queue     → scrape_listing_pw (pool/node)       │
        │         │  match_queue         → match_listing (matching_batch row)  │
        │         │  discovery_queue     → discovery_tasks (unchanged logic)   │
        │         │  orchestrator_queue  → job_chunk_planner (NEW)             │
        │         └─────────────────────────────────────────────────────────────┘
        │                                     │
        │         ┌───────────────────────────▼─────────────────────────────┐
        └─────────│ Object storage (S3-compatible) — replaces FAILURE_DIR     │
                  │ technopolis.py screenshots + large raw_data blobs          │
                  └─────────────────────────────────────────────────────────────┘
```

**Preserves:** `score_product_against_listing`, `apply_scrape_result_to_listing`, `upsert_discovered_products`, `match_service.confirm/reject`, Pydantic schemas in `schemas/scrape_batch.py`, `match_batch.py`, `discovery_batch.py`.

**Replaces:** Mega-tasks `scrape_competitor_products_batch` / `match_competitor_products_batch` as the **primary** scale mechanism (they become planners or are retired in Phase C).

---

## 3. Distributed scraping

### 3.1 Today (`scraping_batch.py`, `scrape_tasks.py`, `technopolis_hybrid.py`)

- **Batch:** One Celery message runs `asyncio.run(_run_batch_scrape_async)` for up to millions of IDs (`_iter_scrape_target_ids`, `CP_BATCH_SIZE=100`).
- **Single:** `scrape_competitor_product` → `scrape_competitor_product_by_id` → `scrape_competitor_product_row` → `asyncio.run` without pool; Playwright spawns per call via `TechnopolisScraper._fetch_html_with_page` (`technopolis.py`).
- **Category path:** `discovery_tasks.scrape_prices_category` loads all `CompetitorProduct.id` for category, then `scrape_competitor_product.delay()` per ID — **unbounded Redis fan-out**.
- **Concurrency:** In-process only — `AdaptiveConcurrencyController` (6–20) inside one worker process.
- **Persist:** `apply_scrape_result_to_listing` updates `latest_*`; optional `PriceSnapshot` if `price_history_enabled` (`scrape_persist.py`).

### 3.2 Target: listing-grain distributed scrape

**Unit of work:** one `competitor_product_id` (same as today’s `scrape_competitor_product` argument).

| Layer | Design |
|-------|--------|
| **Planner** | New task `plan_scrape_job(job_id)` (orchestrator queue) reads `job_chunks` where `chunk_type='scrape'`, enqueues `scrape_listing.delay(cp_id, job_id, layer_hint)` with rate limit per `competitor.domain` |
| **OCC worker** | Wraps existing `fetch_scrape_result_for_listing` → if OCC success, `apply_scrape_result_to_listing` — **no** `TechnopolisPlaywrightPool` |
| **PW worker** | One `TechnopolisPlaywrightPool` per **worker process** (singleton), not per task; reuse `scraping_batch._scrape_one_listing` logic extracted to `scrape_listing_pw` |
| **Layer routing** | Use `scrape_layer_from_result` / `raw_data.scrape_layer` from `scrape_fetch.py` to emit metrics already modeled in `ScrapeBatchMetrics` and `ScrapeTaskStatus` |

**Deprecate at 10M scale:**

- `scrape_prices_category` fan-out — UI and API should only call competitor-level scrape job planner (today’s `POST /competitors/{id}/scrape-all` body in `ScrapeAllBody`).
- Monolithic `scrape_competitor_products_batch` for full-catalog runs — keep temporarily as “compat mode” that only **creates** `job` + chunks, does not scrape inline.

### 3.3 Job chunks (Postgres-driven orchestration)

New tables (Alembic; not in repo today):

```text
jobs
  id, tenant_id, type ENUM(scrape|match|discovery), competitor_id, category_id,
  status, total_units, completed_units, failed_units, created_at, …

job_chunks
  id, job_id, chunk_index, id_range_start, id_range_end (keyset cursor),
  status, celery_task_id nullable, retry_count

job_events
  job_id, ts, event_type, payload JSONB  -- mirrors ScrapeTaskStatus fields
```

**Chunk sizing:** align with existing batch constants:

- Scrape enqueue: **100–500** IDs per chunk (between `scraping_batch.CP_BATCH_SIZE=100` and `matching_batch.CP_BATCH_SIZE=500`).
- Discovery: keep `full_discovery_batch.BATCH_SIZE=1000` for URL upsert loops.

**Idempotency (scrape):**

```text
Redis SET scrape:lock:{competitor_product_id} NX EX 300
```

Before `apply_scrape_result_to_listing`, same semantics as today but prevents duplicate workers from double-scraping during retries.

**Skip rules (unchanged logic, moved to planner SQL):**

Reuse `scraping_batch._scrape_ids_stmt` filters (`only_missing`, `only_stale`, `skip_recent_failures`, `skip_dead_urls`, `HARD_FAIL_SKIP_CODES`) as the **chunk candidate query** — do not reimplement in the planner.

### 3.4 Horizontal scrape workers

| Pool | Queue | Image | `get_settings()` |
|------|-------|-------|------------------|
| `worker-scrape-occ` | `scrape_occ` | Slim Python, no Playwright | `scrape_occ_enabled=true`, `scrape_http_enabled=false` |
| `worker-scrape-pw` | `scrape_pw` | Current `backend/Dockerfile` with Chromium | `scrape_concurrency_max` per **machine** (RAM-bound) |
| `worker-scrape-demo` | `scrape_default` | DemoScraper only | Non-technopolis domains via `registry.get_scraper_for_domain` |

**Compose/K8s:** N replicas of OCC workers, M replicas of PW workers (M small, e.g. 2–4 nodes with 8GB+ RAM). **Anti-pattern today:** 20 Celery processes each launching batch Playwright pools.

### 3.5 Object storage for scrape artifacts

**Today:** `FAILURE_DIR = .../storage/scrape_failures` in `technopolis.py`; local disk in container.

**Target:**

- Store screenshot bytes and large `raw_data` blobs in S3-compatible object storage; DB keeps `raw_data` pointer `{ "s3_key": "...", "scrape_error_code": "..." }`.
- `apply_scrape_result_to_listing` unchanged for `latest_*`; trim hot JSONB on `competitor_products`.

**Cost lever:** At 10M listings, **do not** enable `PRICE_HISTORY_ENABLED` globally (`config.py`) — keep `latest_*` on `competitor_products` as the hot path (`listing_price.py`), same as production default in `docker-compose.yml`.

---

## 4. Distributed matching

### 4.1 Today (`matching_batch.py`, `matching.py`, `routers/competitor_products.py`)

- Batch: `apply_match_for_competitor_product` per row; `_rank_listing_candidates` may call `iter_catalog_batches` for entire `products` table.
- API: `_catalog_for_find_matches` merges all catalog batches into RAM — **cannot survive 10M listings × large catalog**.
- Persist: `_persist_match_plan` deletes non-terminal `ProductMatch` rows then inserts (`match_outcomes.classify_ranked_candidates`).
- Skip: `_should_skip_competitor_product` — one SELECT per listing.

### 4.2 Target: inverted-index match + listing-grain tasks

**Phase B — minimum viable at 10⁶ listings**

1. **SQL candidate retrieval** — extend `fetch_catalog_candidates_for_listing` into `fetch_catalog_candidates_for_listing_v2(db, cp, limit=2000)` using UNION of indexed lookups (already partially in `matching_catalog.py`: EAN, MFR, brand+model, SKU). **Never** call `iter_catalog_batches` from batch or API paths.
2. **Bulk skip** — one query per chunk:

   ```sql
   SELECT competitor_product_id, status FROM product_matches
   WHERE competitor_product_id = ANY(:ids) AND status IN ('confirmed','rejected');
   ```

   Replace `_should_skip_competitor_product` loop.

3. **Distributed unit:** `match_listing.delay(competitor_product_id, job_id, min_score)` runs:

   ```text
   ranked = _rank_listing_candidates_v2(db, cp)  # bounded
   plan = classify_ranked_candidates(ranked, min_score=job.min_score)
   _persist_match_plan(...)
   ```

   Same functions in `matching_batch.py` / `match_outcomes.py` — **extract row handler**, do not duplicate scoring in `matching.py`.

**Phase C — 10M+ listings**

| Mechanism | Purpose |
|-----------|---------|
| **Denormalized `competitor_products.workspace_match_status`** | Updated on match persist + confirm/reject; powers `workspace_query` filter without `best_match_subquery()` window on every page |
| **Optional `match_candidates` staging** | Batch insert top-5 `MatchCandidate` JSON for UI; trim `product_matches.top_candidates` size |
| **pg_trgm / trigram on `products.name`** | Already in migration `20260520_0006` — use for prefilter when EAN/MFR missing, not full table scan |
| **Background “re-match stale”** | Job type `match` with `only_unmatched` from `MatchAllBody` (`schemas/match_batch.py`) — planner only enqueues listings where `workspace_match_status IS NULL` or catalog changed |

**Keep synchronous API for manual review only:**

- `POST /competitor-products/{id}/find-matches` — cap candidates via SQL prefilter; **remove** `_catalog_for_find_matches` full merge.
- `POST /matches/confirm` / `reject` — unchanged (`match_service.py`); emit `job_events` + update denormalized status.

### 4.3 Match worker fleet

- Queue: `match` — **no Playwright**, CPU-bound, scale on core count.
- Concurrency per worker: moderate (4–8) — scoring is Python string ops (`matching.py`), not IO-bound.
- **Do not** run match on scrape workers — today both share default queue in `celery_app.py`.

---

## 5. Realtime UI & WebSocket/SSE

### 5.1 Today (`frontend/app/competitors/page.tsx`)

| Signal | Mechanism |
|--------|-----------|
| Batch scrape progress | `useEffect` → `GET /competitors/scrape-tasks/{scrapeAllTaskId}` every **2000 ms** |
| Batch match progress | `GET /competitors/match-tasks/{matchAllTaskId}` every **2000 ms** |
| Discovery | `GET /competitors/discovery-tasks/{discoveryAllTaskId}` every **2000 ms** |
| Table refresh during job | `setInterval` **5000 ms** → `fetchWorkspacePage` → heavy `paginate_workspace` SQL |
| Types | `ScrapeTaskStatus`, `MatchTaskStatus`, `DiscoveryTaskStatus` in `lib/types.ts` |

Poll handlers use `scrape_task_status_from_meta` / `match_task_status_from_meta` on server (`routers/competitors.py`) — meta shape is the **contract**.

### 5.2 Target: SSE-first, poll fallback

**New endpoint (design):**

```http
GET /api/jobs/{job_id}/events
Accept: text/event-stream
```

**Event payload:** reuse existing Pydantic models — no frontend type churn:

- `event: progress` → body = `ScrapeTaskStatus` | `MatchTaskStatus` | `DiscoveryTaskStatus` (discriminated by `job.type`)
- `event: workspace_tick` → optional lightweight `{ "competitor_id", "category_id", "refresh_hint": true }` to replace blind 5s full page refetch
- `event: done` → final stats currently in `status.result`

**Server pipeline:**

```text
Celery task progress_callback(meta)
  → publish JSON to Redis channel job:{job_id}:events
  → append row to job_events (Postgres, optional)
SSE handler (FastAPI StreamingResponse)
  → subscribe Redis → write SSE frames
```

**Bridge from current code:** `match_tasks.on_progress` and `scrape_batch_tasks.on_progress` already build `meta` dicts — add **one** publisher function called from those callbacks (and from future `job_chunk` tasks).

**WebSocket (optional Phase C):**

- Same Redis channel; WS handler in FastAPI for bidirectional **cancel** (`POST /jobs/{id}/cancel` → revoke Celery chord / mark chunks cancelled).
- `CompetitorsPage` can use WS only if SSE insufficient (multi-tab sync); **default SSE** is enough for progress bars already rendered from `scrapeAllProgress` / `matchAllProgress` state.

### 5.3 Workspace realtime at 10M rows

**Problem:** 5s `fetchWorkspacePage` on million-row competitor is unsustainable (`workspace_query.paginate_workspace`).

**Target UX (same UI components):**

| Mode | Behavior |
|------|----------|
| During job | Progress bar from SSE only; table refresh **on demand** or every 30–60s, not 5s |
| Row-level | After single scrape (`scrapeListing`), patch one row from `GET /competitor-products/{id}` extended with `latest_*` fields (schema extension) |
| Post-job | One `fetchWorkspacePage` + invalidate tree counts (`GET /competitors/tree`) |

**Keyset pagination API change (required for 10M):**

Extend `WorkspaceQueryParams` (`workspace_query.py`) with `cursor: str | null` (base64 of `(latest_scraped_at, id)`) instead of deep `offset` — keep `limit` max 100 from `db/pagination.py`.

---

## 6. Queue orchestration

### 6.1 Today (`celery_app.py`)

```python
include=[
    "app.tasks.scrape_tasks",
    "app.tasks.scrape_batch_tasks",
    "app.tasks.discovery_tasks",
    "app.tasks.match_tasks",
]
# No task_routes, no queues
```

Enqueue points:

| Entry | Function |
|-------|----------|
| `POST /competitors/{id}/scrape-all` | `scrape_competitor_products_batch.delay` |
| `POST /competitors/{id}/match-all` | `match_competitor_products_batch.delay` |
| `POST /competitors/{id}/discover-all-product-urls` | `discover_all_product_urls_for_competitor.delay` |
| `POST /competitor-categories/{id}/scrape-prices` | `scrape_prices_category.delay` → N × `scrape_competitor_product.delay` |
| `POST /jobs/scrape-product/{id}` | `scrape_competitor_product.delay` |

### 6.2 Target queue topology

| Queue | Tasks | Worker deployment |
|-------|-------|-----------------|
| `orchestrator` | `plan_scrape_job`, `plan_match_job`, `finalize_job` | Low CPU, 2–4 processes |
| `scrape_occ` | `scrape_listing` (OCC path) | High replica count |
| `scrape_pw` | `scrape_listing_pw` | Few fat nodes |
| `match` | `match_listing` | CPU autoscaled |
| `discovery` | existing `discovery_tasks.*` | I/O bound, separate from scrape |
| `default` | `scrape_competitor_product` (legacy) | Drain-only during migration |

**Celery configuration additions (design):**

```python
task_routes = {
    "app.tasks.scrape_tasks.scrape_listing": {"queue": "scrape_occ"},
    "app.tasks.match_tasks.match_listing": {"queue": "match"},
    "app.tasks.orchestrator.plan_scrape_job": {"queue": "orchestrator"},
    ...
}
task_acks_late = True
worker_prefetch_multiplier = 1   # critical for scrape_pw fairness
result_expires = 3600
broker_transport_options = {"visibility_timeout": 3600}
```

**Chord / group pattern for job completion:**

```text
plan_scrape_job
  → group(scrape_listing.si(cp_id) for cp_id in chunk) 
  → chord callback finalize_scrape_job(job_id)
```

`finalize_scrape_job` aggregates counters into `jobs` row — same fields as today’s batch return dict from `run_batch_scrape_competitor_products`.

### 6.3 Async APIs (HTTP)

**Today:** All enqueue endpoints return **202** with `task_id` (`DiscoverQueued`, `ScrapeAllQueued`, `MatchAllQueued`).

**Target:**

| Endpoint | Change |
|----------|--------|
| `POST /competitors/{id}/scrape-all` | Creates `jobs` row; returns `{ "job_id", "task_id" }` where `task_id` is orchestrator Celery id; poll migrates to `/jobs/{job_id}/events` |
| `GET /competitors/scrape-tasks/{id}` | Deprecated wrapper → reads `job_events` or Redis cache for backward compat |
| `POST /competitor-products/{id}/find-matches` | **202** + `job_id` for large catalogs; **200** only when prefilter returns &lt;50 candidates synchronously |
| `GET /competitor-categories/{id}/products` | Read-only; **cursor** params; served from replica |

FastAPI remains **sync SQLAlchemy** in Phase B; Phase C optional `asyncpg` for SSE fan-out only — **not required** if Redis pub/sub decouples.

---

## 7. PostgreSQL scaling (10M+ `competitor_products`)

### 7.1 Table strategy

| Table | 10M+ approach |
|-------|----------------|
| `competitor_products` | **Partition by LIST (`competitor_id`)** or hash on `competitor_id` — every batch query in `scraping_batch` / `matching_batch` already filters `competitor_id` |
| `product_matches` | Partition by `competitor_product_id` hash or attach to same competitor_id denormalized column (add `competitor_id` on match row for partition pruning) |
| `price_snapshots` | **Do not grow** at 10M without tiering — keep `PRICE_HISTORY_ENABLED=false`; if enabled, monthly partitions + archive to object storage |
| `products` | Typically 10⁴–10⁶ — stays non-partitioned; heavy indexing from `20260520_0006` |
| `jobs`, `job_chunks`, `job_events` | New; partition `job_events` by month |

### 7.2 Query rewrites (tie to files)

| Current | Future |
|---------|--------|
| `paginate_workspace` `COUNT(*)` on filtered subquery | **Approximate count** (pg_stat) or cached `competitor_category.product_count` + filter bitmap; exact count only page 1 |
| `best_match_subquery()` window on every page | **Read `workspace_match_status`** column on `competitor_products`; join `product_matches` only for detail drawer |
| `_iter_scrape_target_ids` OFFSET | **Keyset:** `WHERE (latest_scraped_at, id) < (:cursor)` ORDER BY `latest_scraped_at DESC NULLS LAST, id DESC` — index `ix_competitor_products_competitor_latest_scraped` exists (`20260521_0007`) |
| `competitor_overview_service` full table count | Remove or scope per `competitor_id` |

### 7.3 Read path vs write path

```text
Primary (writes)  ← PgBouncer transaction mode ← Celery scrape/match workers, API POST
Replica(s) (reads) ← PgBouncer session mode    ← workspace_query, price_comparison, tree
```

**`price_comparison_service.build_price_comparison_page`** — already batches `product_ids` and `all_cp_ids`; safe on replica.

**`build_competitor_forest`** — cache tree per `competitor_id` in Redis (TTL 60s); invalidate on `discover_categories_competitor` completion.

### 7.4 Connection pools

**Today:** `database.py` — no pool limits.

**Target:**

- API uvicorn: `pool_size=10`, `max_overflow=20` per pod × few pods → through PgBouncer max **~100** client connections.
- Celery worker: `pool_size=2` per child process — workers are task-heavy, not connection-heavy if commits are short (already every `scrape_batch_commit_size` in `scraping_batch.py`).

---

## 8. Redis scaling

### 8.1 Today

- Single URL `redis://redis:6379/0` for broker **and** backend (`docker-compose.yml`, `config.effective_celery_broker/backend`).
- `AsyncResult(task_id)` poll reads result backend on every UI poll.

### 8.2 Target topology

| Redis logical DB / keyspace | Use |
|----------------------------|-----|
| Broker DB | Celery queues only; short messages (`cp_id`, `job_id`) |
| Result backend | Disabled or `result_expires=300` — **do not** store large batch dicts at 10M scale |
| `job:{uuid}:events` pub/sub | SSE fan-out |
| `job:{uuid}:snapshot` HASH | Latest progress (mirror `ScrapeTaskStatus` fields) — single read for poll fallback |
| `scrape:lock:{cp_id}` | SET NX EX |
| `tree:{competitor_id}` | Cached JSON from `build_competitor_forest` |

**At 10M scale:** Redis Cluster or managed Redis with **≥8GB** if pub/sub fan-out to hundreds of SSE connections; broker memory bounded by **queue depth limits** (max in-flight scrape messages = worker_throughput × SLA).

**Poll elimination impact:**

100 users × 0.5 poll/s × 2 endpoints = **100 req/s** removed from API+Redis — replaced by **100 SSE connections** × 1 event/3s ≈ 33 events/s publish (scrape progress interval `scrape_progress_interval_sec=3` in `config.py`).

---

## 9. Horizontal workers & Docker/K8s

### 9.1 Today (`docker-compose.yml`)

- `backend`: uvicorn `--reload`, bind mount `./backend`
- `celery_worker`: **one** service, `celery -A app.celery_app worker`
- `frontend`: production `next build` image

### 9.2 Target services (same codebase, multiple Compose profiles)

```yaml
# Conceptual — not in repo today
celery_worker_orchestrator:
  command: celery -A app.celery_app worker -Q orchestrator -c 2
celery_worker_scrape_occ:
  command: celery -A app.celery_app worker -Q scrape_occ -c 16
  # slim image without playwright
celery_worker_scrape_pw:
  command: celery -A app.celery_app worker -Q scrape_pw -c 2
  deploy:
    resources:
      limits:
        memory: 8G
  shm_size: '1gb'   # Playwright — technopolis_playwright_pool.py uses --disable-dev-shm-usage but host shm still helps
celery_worker_match:
  command: celery -A app.celery_app worker -Q match -c 8
celery_worker_discovery:
  command: celery -A app.celery_app worker -Q discovery -c 4
```

**API:** `backend` replicas behind load balancer; **sticky sessions not required** if SSE uses `job_id` subscription (any pod can read Redis).

**Migration note:** Celery workers **do not** auto-reload (`README.md`); deploy rolling restart on task module changes.

---

## 10. Event-driven architecture

### 10.1 Event types (aligned with existing meta dicts)

| `event_type` | Source today | Payload |
|--------------|--------------|---------|
| `scrape.progress` | `scraping_batch._report` | Fields in `ScrapeTaskStatus` (`schemas/scrape_batch.py`) |
| `scrape.listing_done` | `apply_scrape_result_to_listing` outcome | `{ cp_id, outcome, latest_price }` |
| `match.progress` | `matching_batch._report` | `MatchTaskStatus` fields |
| `match.listing_done` | `_persist_match_plan` | `{ cp_id, status, match_score }` |
| `discovery.progress` | `full_discovery_batch._report` | `DiscoveryTaskStatus` / `FullDiscoveryStats` |
| `job.completed` | chord finalize | Same as current `async_result.result` dict |

### 10.2 Outbox pattern (optional Phase C)

On `db.commit()` in workers:

```text
INSERT job_events (...)  -- same transaction as listing update
```

Async publisher (sidecar or thread) pushes to Redis — avoids lost events if Redis blips.

### 10.3 Downstream consumers (future, same repo boundaries)

- **Price alerts** — subscribe to `scrape.listing_done` where `latest_price` changed vs previous
- **Analytics** — batch export `job_events` to columnar store (not in MVP codebase)

**No Kafka required for Phase B** — Postgres `job_events` + Redis pub/sub sufficient for hundreds of UI users.

---

## 11. Fault tolerance & retries

### 11.1 Today

- Celery: no `autoretry` on tasks.
- Playwright: one retry in `technopolis_hybrid._fetch_playwright_with_retry`.
- Batch: row failure logged in `errors` list; continues (`scraping_batch`, `matching_batch`).
- URL health: `url_health.update_url_health_after_scrape`, `is_dead` skip in batch (`scraping_batch`).

### 11.2 Target policy

| Failure class | Action |
|---------------|--------|
| OCC timeout / 5xx | Retry `scrape_listing` max 3, exponential backoff, queue `scrape_occ` |
| Playwright timeout | Retry once on PW worker (existing hybrid behavior); then mark `latest_scrape_error_code` via `classify_scrape_failure` |
| Hard fail codes (`HARD_FAIL_SKIP_CODES` in `scrape_errors.py`) | No retry; planner skips via existing `skip_recent_failures` |
| Match exception | Retry chunk id list only; bulk skip confirmed/rejected |
| Worker OOM | `task_acks_late` + visibility timeout — message returns to queue; **scrape lock** prevents duplicate apply |

**Job cancellation:**

- User clicks cancel → `jobs.status=cancelled` → orchestrator revokes pending chunks; workers check `job_id` status before scrape.

**Idempotent persist:**

- Scrape: `latest_*` overwrite — safe.
- Match: `_persist_match_plan` delete+insert — safe if delete scope unchanged.

---

## 12. Caching

| Cache | Key | Invalidation | Replaces |
|-------|-----|--------------|----------|
| Category tree | `tree:{competitor_id}` | Category discovery task done | `build_competitor_forest` full load every refresh |
| Workspace count (approx) | `ws:count:{competitor_id}:{filter_hash}` | TTL 30s during jobs | `paginate_workspace` count query |
| Catalog prefilter bloom | `catalog:ean:{ean}` → product ids | On `import_products_from_xlsx` | Repeated EAN lookups in `matching_catalog` |
| Job snapshot | `job:{id}:snapshot` | Each progress event | `AsyncResult.info` |

**Not cached:** `score_product_against_listing` inputs — always fresh listing `ean`/`title` from DB row.

**Today’s in-process cache:** `_category_path_cache` in `workspace_query.py` — keep per request; add Redis for cross-request tree paths at scale.

---

## 13. Cost optimization (this stack, this workload)

| Lever | Rationale in this codebase |
|-------|----------------------------|
| **Maximize OCC path** | `scrape_technopolis_occ` in `technopolis_hybrid.py` — cheapest; scale `worker-scrape-occ` horizontally |
| **Minimize Playwright fleet** | Pool per node (`TechnopolisPlaywrightPool`); fat nodes only for fallback rate &lt;5% |
| **Kill `PRICE_HISTORY_ENABLED`** at 10M | Avoid `price_snapshots` explosion (`scrape_persist.py`) |
| **Shrink JSONB** | Store `top_candidates` summary (3 items) in DB; full list in object storage if needed |
| **Right-size Redis** | Short broker messages; no large result blobs — progress in `job_events` / snapshot HASH |
| **Postgres disk** | Partition + autovacuum tuning on `competitor_products`; trgm indexes only where `workspace_query` search used |
| **UI cost** | SSE vs 2s poll — fewer API replicas |
| **Discovery cap** | Raise `DEFAULT_MAX_PRODUCTS` only with streaming ingest — today loads sitemap into memory (`technopolis_full_discovery.py`) |
| **DemoScraper competitors** | Zero infra cost — do not assign Playwright workers |

**Unit economics example (order of magnitude):**

- 10M listings × daily OCC scrape @ ~200ms ≈ 23k OCC-hours → OCC worker fleet, not 10M Playwright sessions.
- Playwright only for OCC miss + discovery category crawl (`discover_technopolis_category_nodes` in `discovery_tasks`).

---

## 14. Migration roadmap (code-aware)

### Phase A — Harden current architecture (10⁵ listings, minimal schema)

| # | Change | Touches |
|---|--------|---------|
| A1 | `task_routes` + 4 queues in `celery_app.py` | `celery_app.py`, Compose |
| A2 | Replace `scrape_prices_category` with `scrape_competitor_products_batch.delay(category_id=…)` | `discovery_tasks.py`, `competitor_categories.py` |
| A3 | `scrape_competitor_product_row` always use pool when technopolis | `scrape_persist.py`, `technopolis_hybrid.py` |
| A4 | Bulk `_should_skip` in `matching_batch` | `matching_batch.py` |
| A5 | Remove full-catalog merge in `_catalog_for_find_matches` | `competitor_products.py` |
| A6 | Redis scrape lock + `result_expires` | new `services/redis_lock.py`, `celery_app.py` |
| A7 | PgBouncer + pool_size in `database.py` | `database.py`, Compose |

### Phase B — Orchestration (10⁶ listings)

| # | Change | Touches |
|---|--------|---------|
| B1 | Alembic: `jobs`, `job_chunks`, `job_events` | `alembic/versions/` |
| B2 | `plan_scrape_job`, `plan_match_job`, `match_listing`, `scrape_listing` tasks | `tasks/orchestrator.py` (new), split batch modules |
| B3 | Enqueue returns `job_id`; compat poll maps job → events | `routers/competitors.py`, `schemas/` |
| B4 | SSE `GET /jobs/{id}/events` | `routers/jobs.py`, `CompetitorsPage` |
| B5 | Keyset workspace pagination | `workspace_query.py`, `CompetitorsPage` query builder |
| B6 | `workspace_match_status` column + backfill | model `competitor_product.py`, `workspace_query._effective_status_expr` |
| B7 | S3 for `FAILURE_DIR` screenshots | `technopolis.py`, `config.py` |

### Phase C — 10M+ listings

| # | Change | Touches |
|---|--------|---------|
| C1 | Partition `competitor_products` by `competitor_id` | Alembic, all `competitor_id` queries (already scoped) |
| C2 | Retire inline mega-batch; orchestrator-only | `scraping_batch.run_batch_*`, `matching_batch.run_batch_*` |
| C3 | Read replicas for workspace + price comparison | `database.py` read engine, `workspace_query` |
| C4 | Match candidate SQL v2 (no Python catalog scan) | `matching_catalog.py`, `matching_batch._rank_listing_candidates` |
| C5 | Optional WebSocket cancel channel | `routers/jobs.py`, UI |
| C6 | Streaming discovery (no 50k URL list in RAM) | `full_discovery_batch.py`, `technopolis_full_discovery.py` |

---

## 15. What stays unchanged at 10M+

These are **strengths** of the current design — carry forward:

| Component | Why keep |
|-----------|----------|
| `latest_*` on `competitor_products` | Workspace + comparison read path (`listing_price.py`, `workspace_query`) |
| `score_product_against_listing` | Deterministic, debuggable matching (`matching.py`) |
| `classify_ranked_candidates` | Clear business outcomes (`match_outcomes.py`) |
| `TechnopolisPlaywrightPool` lifecycle | `new_page` + `close` per URL — correct for long-running workers |
| `AdaptiveConcurrencyController` | Per-node protection — move inside PW worker, not global batch |
| Pydantic task status schemas | SSE contract (`scrape_batch.py`, `match_batch.py`, `competitor_tree.py` discovery) |
| `uq_competitor_product_url` | Idempotent discovery (`20260520_0004`) |
| Manual `match_service.confirm/reject` | Human-in-the-loop at scale |

---

## 16. Anti-patterns to avoid (explicitly rejected for this product)

| Anti-pattern | Why it fights this codebase |
|--------------|----------------------------|
| One giant `scrape_competitor_products_batch` per 10M run | Already blocks one worker; `asyncio.run` for hours (`scraping_batch.py`) |
| Full-catalog `find-matches` sync API | `_catalog_for_find_matches` RAM (`competitor_products.py`) |
| 2s `AsyncResult` polling at 100+ users | `competitors/page.tsx` + Redis read amplification |
| Storing full scrape history for all listings | `PRICE_HISTORY_ENABLED` + `price_snapshots` |
| Playwright on every worker replica | Single-scrape path spawns browser today (`technopolis.py`) — must not scale horizontally as-is |
| Replacing Celery with custom k8s jobs only | Loses `update_state(PROGRESS)` pattern already wired to UI types |

---

## 17. Success metrics (production readiness)

| Metric | Target at 10M listings |
|--------|------------------------|
| Workspace page P95 (keyset, replica) | &lt; 300 ms |
| SSE progress latency | &lt; 1 s behind worker |
| Scrape throughput (OCC fleet) | SLA-defined per competitor (e.g. full catalog &lt; 24h) |
| Match throughput | &lt; 24h full re-match with SQL prefilter only |
| Redis memory | Stable under queue depth cap |
| Postgres connections | Flat via PgBouncer |
| Cost per 1M scrapes (OCC) | Track `occ_api_success` / `playwright_fallback` from `ScrapeBatchMetrics` |

Instrument using fields **already exposed** in `ScrapeTaskStatus` (`occ_api_success`, `playwright_fallback`, `products_per_minute`) — extend `job_events.payload` with same keys for continuity.

---

## 18. Quick reference — today → future mapping

| Today | Future role |
|-------|-------------|
| `scrape_competitor_products_batch` | `plan_scrape_job` + N × `scrape_listing` |
| `match_competitor_products_batch` | `plan_match_job` + N × `match_listing` |
| `scrape_competitor_product` | `scrape_listing` with lock |
| `scrape_prices_category` | **Removed** — use job planner |
| `GET /competitors/scrape-tasks/{id}` | SSE `/jobs/{id}/events` + deprecated poll |
| `paginate_workspace` OFFSET | Keyset + `workspace_match_status` |
| `best_match_subquery()` | Detail-only; list uses denormalized column |
| `FAILURE_DIR` local | S3-compatible object storage |
| `AsyncResult` + Redis result | `job_events` + pub/sub snapshot |

---

*End of future production architecture design.*
