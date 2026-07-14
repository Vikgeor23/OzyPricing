# Scalability & Realtime Architecture Audit

**Project:** Pricing Monitor  
**Audit date:** 2026-05-22  
**Mode:** Read-only (no code changes)  
**Target scale (design lens):** millions of competitor listings, thousands of scrape jobs, hundreds of concurrent UI users, Celery workers on multiple Linux hosts.

This document is grounded in the **current implementation** only. File paths and symbols refer to the repository as audited.

---

## Current scalability level

| Dimension | Today (as implemented) | Order-of-magnitude comfort zone |
|-----------|------------------------|----------------------------------|
| `competitor_products` rows | Batched writes (1k discovery, 100 scrape batch), paginated reads (max 100/page) | **~10⁴–10⁵** listings per competitor with tuned Postgres |
| Concurrent scrape throughput | One batch task holds a worker; in-process `asyncio.gather` + `AdaptiveConcurrencyController` (6–20) | **~10²–10³** listings/hour per worker (site-dependent) |
| Concurrent match throughput | Sync CPU scoring in Celery; optional full-catalog scan per listing | **~10²–10⁴** listings/hour per worker (depends on catalog size & prefilter) |
| API + UI | Sync FastAPI + 2s Celery poll + 5s workspace refresh | **~10¹–10²** active users with batch jobs (poll amplification) |
| Horizontal workers | Possible but **no queue isolation, no idempotency, no distributed locks** | Multiple workers help **only** for independent single-listing tasks |
| Realtime | HTTP polling only; no WebSocket/SSE | Not realtime; “live” = Redis `PROGRESS` meta + REST poll |

**Verdict:** Architecture is a **single-tenant MVP** optimized for Technopolis-scale pilots (thousands to low tens of thousands of URLs), not yet a distributed system for millions of listings and hundreds of simultaneous operators without structural changes.

---

## Current bottlenecks

1. **Matching CPU × catalog size** — `matching_batch._rank_listing_candidates()` may scan the entire `products` table in 500-row batches per listing when `fetch_catalog_candidates_for_listing()` returns `None` (`services/matching_catalog.py`, `services/matching_batch.py`).
2. **Per-listing DB round-trips in batch match** — `_should_skip_competitor_product()` loads all `ProductMatch` rows per `competitor_product_id` inside the per-row loop (`matching_batch.py`).
3. **Workspace query cost** — `paginate_workspace()` runs `best_match_subquery()` (window over `product_matches`) + `COUNT(*)` on a filtered subquery + `OFFSET` page (`workspace_query.py`, `db/latest_price.py`).
4. **Long-lived Celery tasks** — `run_batch_scrape_competitor_products()` wraps `asyncio.run(_run_batch_scrape_async(...))` (`scraping_batch.py`), blocking one worker slot for the full job duration.
5. **Category scrape fan-out** — `scrape_prices_category()` loads **all** listing IDs then `scrape_competitor_product.delay()` per ID (`discovery_tasks.py`) — Redis and worker churn.
6. **Single-listing Playwright lifecycle** — OCC miss path uses `TechnopolisScraper._fetch_html_with_page()` → **new** `async_playwright()` + browser per invocation (`technopolis.py`, `scrape_persist.scrape_competitor_product_row()`).
7. **Frontend poll storm** — `CompetitorsPage` 2s task polls + 5s workspace refresh during batch jobs (`frontend/app/competitors/page.tsx`).
8. **One Redis, one default queue** — broker and result backend share `redis_url` (`celery_app.py`, `config.py`); no dedicated queues or result TTL configuration in code.
9. **No application-level cache** — only in-request dict caches (`_category_path_cache` in `workspace_query.py`); Redis used for Celery only.
10. **Synchronous full-catalog API** — `_catalog_for_find_matches()` merges all batches into one Python list (`routers/competitor_products.py`).

---

## Immediate scale risks

| Risk | Trigger | Symptom |
|------|---------|---------|
| Worker starvation | Large `scrape_competitor_products_batch` or `match_competitor_products_batch` | Queue latency spikes; single-listing scrapes wait hours |
| Redis memory | Thousands of `scrape_competitor_product` messages + large `PROGRESS` meta | Redis OOM/eviction; lost task state |
| Postgres bloat | `PRICE_HISTORY_ENABLED=true` or huge `top_candidates` JSONB | Table/index bloat, slow vacuum, large pages |
| Deep `OFFSET` | Workspace page 10 000+ on million-row competitor | Multi-second page loads |
| Duplicate scrape/match | Same listing enqueued from UI + batch + category fan-out | Wasted CPU; last-write-wins on `latest_*` / `ProductMatch` |
| Browser exhaustion | Many workers each running Playwright batch at `scrape_concurrency_max=20` | OOM, `/dev/shm` pressure in containers |
| Poll-induced API meltdown | 100 users × (2 poll endpoints + 5s workspace) during jobs | API CPU/connection pool saturation |

---

## Estimated breaking points

Assumptions: single Celery worker, Postgres 16 with migrations through `20260523_0012`, Technopolis OCC enabled (`SCRAPE_OCC_ENABLED=true`), catalog **10⁵** `products`, one competitor **10⁶** `competitor_products`.

| Workload | Rough breaking point | Why (code path) |
|----------|----------------------|-----------------|
| Batch match (no EAN prefilter) | **~5×10³–10⁴** listings / worker-day | O(listings × catalog_batches) scoring in `_rank_listing_candidates` |
| Batch match (EAN/MFR prefilter) | **~10⁵–10⁶** listings / worker-day | Bounded `PREFILTER_LIMIT` 500 per signal (`matching_catalog.py`) |
| Batch scrape (OCC hit rate high) | **~10⁴–10⁵** listings / worker-day | `AdaptiveConcurrencyController` + OCC fast path (`technopolis_hybrid.py`) |
| Batch scrape (OCC miss → Playwright) | **~10³** listings / worker-day | Pool helps batch; single-task path spawns browser (`technopolis.py`) |
| Workspace UI pagination | **~10⁵–10⁶** rows / competitor (deep offset) | `OFFSET` + `COUNT` on joined/filtered subquery (`paginate_workspace`) |
| Full sitemap discovery | **~5×10⁴** URLs / run cap | `DEFAULT_MAX_PRODUCTS = 50_000` (`technopolis_full_discovery.py`) |
| `GET /competitors/tree` | **~10³–10⁴** categories / competitor | Loads all categories into memory (`competitor_tree_service.build_competitor_forest`) |
| Concurrent UI users (active batch jobs) | **~20–50** | 2s `AsyncResult` polls + 5s workspace refetch per user |
| Category `scrape-prices` | **~10³** queued tasks | Linear `delay()` loop (`discovery_tasks.scrape_prices_category`) |

These are **order-of-magnitude** estimates, not benchmarks. The repo ships `scripts/explain_core_queries.py` for Postgres plans on a populated DB.

---

## DB audit

### Connection & pool

**File:** `backend/app/database.py`

```python
engine = create_engine(settings.database_url, pool_pre_ping=True)
```

- No `pool_size`, `max_overflow`, or `pool_timeout` — SQLAlchemy defaults (~5 connections per process).
- FastAPI (uvicorn) + each Celery worker process = **separate pools**. At 10 API workers + 20 Celery workers → up to **150** DB connections if not capped externally.
- **Risk at scale:** connection storms under hundreds of API users; no PgBouncer configuration in repo.

### Schema & JSONB

| Table | JSONB columns | Scale note |
|-------|---------------|------------|
| `competitor_products` | `specs_json`, `raw_identifiers` | Row-wide storage; repeated scrape updates |
| `product_matches` | `match_warnings`, `top_candidates` | `top_candidates` stores up to 5 candidates per row (`match_outcomes.candidate_to_dict`) — grows with batch match |
| `price_snapshots` | `raw_data` | Unbounded history if `price_history_enabled` (`config.py`, `scrape_persist.py`) |

### Indexes present (migrations)

**Workspace / listing access** (`20260520_0004`, `20260521_0007`, `20260520_0006`):

- `uq_competitor_product_url` (`competitor_id`, `url`)
- `ix_competitor_products_competitor_category`
- `ix_competitor_products_latest_scraped_at`
- `ix_competitor_products_competitor_latest_scraped` (`competitor_id`, `latest_scraped_at` DESC)
- `ix_competitor_products_category_latest_scraped`
- `ix_competitor_products_title_trgm` (GIN, Postgres)
- `ix_product_matches_cp_status` (`competitor_product_id`, `status`)

**Matching / catalog** (`20260520_0005`, `20260520_0006`):

- `products`: `ean`, `manufacturer_code`, `model`, `name` trgm
- `product_matches`: `status`, `(product_id, status)`, `match_score`

### Missing / weak indexes (for stated target scale)

| Query pattern | Location | Gap |
|---------------|----------|-----|
| Batch iteration `ORDER BY created_at DESC OFFSET n` | `matching_batch._iter_competitor_product_ids`, `scraping_batch._iter_scrape_target_ids` | No composite `(competitor_id, created_at DESC)` or keyset pagination — deep offset degrades |
| Filter `latest_scrape_status` + `latest_scraped_at` (stale/failed skip) | `scraping_batch._scrape_ids_stmt` | Partial indexes on `(competitor_id, latest_scrape_status, latest_scraped_at)` not defined |
| Workspace filter `status = needs_review` etc. | `_effective_status_expr` + `WHERE` | Status is **computed** from `product_id` + `best_match` — cannot use simple index on `product_matches.status` alone for all cases |
| `technopolis_product_code` lookup | `full_discovery_batch._existing_by_product_codes` | Column indexed on model (`competitor_product.py`) — OK |
| `Product.order_by(Product.name)` | `price_comparison_service.build_price_comparison_page` | `ix_products_name_lower` / trgm exist — OK for search, less for sort at huge offset |

### N+1 and query batching

| Path | Pattern | Assessment |
|------|---------|------------|
| `price_comparison_service.build_price_comparison_page` | Batched `product_ids` → `CompetitorProduct` + `joinedload(competitor)` | **Good** — explicit batching, documented “no N+1” in `dashboard_service.py` comment lineage |
| `scraping_batch._run_batch_scrape_async` | `joinedload(CompetitorProduct.competitor)` per 100-id batch | **Good** within batch |
| `matching_batch.apply_match_for_competitor_product` | `_should_skip` query **per listing** | **N+1** over batch size 500 |
| `workspace_query._row_to_schema` → `display_category_path` | Uses `assigned_path_cache` for category paths | **Mostly OK**; breadcrumb path may call `category_path_names(db, …)` per row if cache miss (`competitor_category_builder.py`) |
| `competitor_tree_service.build_competitor_forest` | 1 query per competitor for all categories | **OK** for few competitors; **heavy** if many competitors × huge category tables |

### Full table scans

| Endpoint / job | Scan risk |
|----------------|-----------|
| `GET /competitor-products/overview` | `count(*)` on full `competitor_products` (`competitor_overview_service.py` L44) |
| `GET /products`, price comparison | `count(*)` on full `products` (`price_comparison_service.py` L167) |
| Batch match without prefilter | Repeated `select(Product).order_by(Product.id).offset` — full catalog passes |
| `best_match_subquery()` | Window over **all** matches per workspace query — Postgres must partition by `competitor_product_id` |

### Workspace pagination scalability

**Files:** `services/workspace_query.py`, `schemas/workspace_page.py`

- Page size capped at **100** (`db/pagination.py` `MAX_PAGE_LIMIT`).
- Every page request:
  1. Builds `best_match_subquery()` (ranked window on `product_matches`).
  2. `COUNT(*)` on filtered join subquery.
  3. `ORDER BY latest_scraped_at DESC NULLS LAST, created_at DESC` + `LIMIT/OFFSET`.

**At millions of rows:**

- `OFFSET` beyond ~10⁴–10⁵ becomes dominant cost.
- `COUNT(*)` with `search` + `status` filters on ILIKE title/url (`_apply_workspace_filters`) forces large scans even with trgm (search helps; status filter does not use a materialized column).

**Tests:** `tests/test_workspace_pagination.py` — correctness on small SQLite, not load.

### Batching (DB writes)

| Job | Batch size | Commit strategy |
|-----|------------|-----------------|
| Scrape batch | 100 IDs / round (`CP_BATCH_SIZE`) | Commit every `scrape_batch_commit_size` (default 20) (`scraping_batch.py`) |
| Match batch | 500 IDs / round (`CP_BATCH_SIZE`) | Commit after each 500-listing batch (`matching_batch.py`) |
| Full discovery | 1000 URLs (`BATCH_SIZE` in `full_discovery_batch.py`) | Per-batch commit in loop |

**Long-running safety:** Batch scrape/match hold DB transactions only around commits — good. Single listing scrape commits once (`scrape_competitor_product_by_id`).

---

## Celery audit

### Configuration (`backend/app/celery_app.py`)

```python
celery_app = Celery("price_monitor", broker=..., backend=..., include=[...])
celery_app.conf.update(task_serializer="json", ...)
```

**Not configured in codebase:**

- `task_queues` / `task_routes` — **single default queue**
- `worker_prefetch_multiplier`, `task_acks_late`, `task_reject_on_worker_lost`
- `task_time_limit`, `task_soft_time_limit`
- `result_expires`, `result_extended`
- `broker_transport_options` (visibility timeout)
- `autoretry_for`, `retry_backoff` on tasks

### Task inventory & worker binding

| Task | File | `bind=True` | PROGRESS updates |
|------|------|-------------|------------------|
| `scrape_competitor_product` | `tasks/scrape_tasks.py` | No | No |
| `scrape_competitor_products_batch` | `tasks/scrape_batch_tasks.py` | Yes | `on_progress` → `update_state` |
| `match_competitor_products_batch` | `tasks/match_tasks.py` | Yes | Yes |
| `discover_all_product_urls_for_competitor` | `tasks/discovery_tasks.py` | Yes | Yes |
| `discover_categories_competitor` | `discovery_tasks.py` | No | No |
| `discover_products_category` | `discovery_tasks.py` | No | No |
| `scrape_prices_category` | `discovery_tasks.py` | No | No (fans out) |
| `find_matches_category` | `discovery_tasks.py` | No | No (sync batch inside task) |

### Queue isolation

**Current:** All tasks compete on the default queue. A million-listing `scrape_competitor_products_batch` **monopolizes** workers that could serve `scrape_competitor_product` or API-triggered work.

**Implication for multi-server:** Scaling workers without queue separation **does not** isolate scrape vs match vs discovery SLA.

### Worker concurrency strategy

- Celery worker concurrency = OS default (often **# CPUs**) per process.
- Inside batch scrape, concurrency is **second layer**: `AdaptiveConcurrencyController` (`services/adaptive_concurrency.py`) caps in-flight async scrapes at 6–20 (`config.py` `scrape_concurrency_*`).
- **Effect:** One batch task = one worker child process running `asyncio.run` with up to 20 concurrent HTTP/OCC/Playwright operations — **not** 20 separate Celery tasks.

**`find_matches_category`:** Runs `apply_best_matches_for_category` → `run_batch_match_competitor_products(..., only_unmatched=False)` — same heavy path as match-all, **no** progress callback wired from router (task does not pass `progress_callback`).

### Retry architecture

| Layer | Retries |
|-------|---------|
| Celery task | **None** — failed task returns error dict or raises; no automatic requeue |
| Playwright | `_fetch_playwright_with_retry` once on timeout (`technopolis_hybrid.py`) |
| HTTP client | No global retry policy in `scrape_fetch` |
| DB commit failure | Row-level `rollback` in batch scrape; batch continues (`scraping_batch.py`) |

**Fault tolerance:** Partial batch completion is possible (some rows failed, committed others). No dead-letter queue in code.

### Duplicate task protection

- **None.** Same `competitor_product_id` can be queued via:
  - `POST /jobs/scrape-product/{id}`
  - `POST /competitor-products/{id}/scrape`
  - `scrape_prices_category` fan-out
  - Overlapping batch scrape jobs

No Redis lock, no `task_id` dedup key, no DB “scrape in progress” flag.

### Idempotency

| Operation | Idempotent? |
|-----------|-------------|
| URL upsert | **Yes** — `uq_competitor_product_url` (`competitor_product_service.upsert_competitor_product_url`) |
| Scrape persist | **Mostly** — overwrites `latest_*`; optional new `PriceSnapshot` row if history on |
| Batch match persist | **Deletes** non-confirmed/rejected matches then inserts (`_persist_match_plan`) — re-run replaces prior auto/needs_review rows |
| Confirm match | **Upsert** by `(product_id, competitor_product_id)` (`match_service.upsert_match_and_link_product`) |

Re-running batch match with `only_unmatched=True` skips listings with **any** existing match row (`_should_skip` “already_matched”) — not idempotent for “refresh scores”.

### Long-running task safety

- Batch scrape reports progress every `scrape_progress_interval_sec` (3s) but can run **hours** — no heartbeat beyond Celery `PROGRESS`.
- Worker kill mid-batch: last committed batch preserved; in-flight batch may rollback per-row errors only.
- `asyncio.run()` per batch task: **new event loop per task** — acceptable; no loop leak across tasks.

### Horizontal scaling readiness (workers)

| Works today | Breaks or needs design |
|-------------|----------------------|
| Multiple workers consuming **single-listing** scrape tasks | Duplicate scrapes without locking; Playwright per task on OCC miss |
| Multiple workers on **batch** scrape | Safe if **one batch job per competitor scope** enforced externally; else duplicate work |
| Multiple workers on batch match | Duplicate DB deletes/inserts racing on same `competitor_product_id` |
| `PROGRESS` meta in Redis | Last writer wins if duplicate task IDs (should not happen) / duplicate tasks with different IDs |

**Distributed locking:** **Not implemented.** Needed for: same listing scrape, same competitor batch jobs, discovery upsert races (mitigated by unique URL constraint).

---

## Redis audit

### Roles

| Use | Config |
|-----|--------|
| Celery broker | `effective_celery_broker` → `CELERY_BROKER_URL` or `redis_url` |
| Result backend | `effective_celery_backend` — same Redis DB `0` in Compose |

### Bottlenecks

1. **PROGRESS payload size** — `scraping_batch._report` merges `ScrapeBatchMetrics.as_dict()` (many counters) into meta every 3s (`scraping_batch.py`). Large `errors` list truncated to 20 in meta — good.
2. **Poll amplification** — Each `GET /competitors/scrape-tasks/{id}` calls `AsyncResult(task_id)` (`competitors.py`) — Redis read per poll × users.
3. **Category scrape fan-out** — N task messages for N listings — broker list length grows linearly.
4. **No result TTL** in `celery_app.conf` — completed task results accumulate unless Redis eviction policy trims them.
5. **Single DB index** — broker and backend share instance; no separation for ops tuning.

### Caching

**Redis is not used for application cache** (no cache-aside for workspace, tree, or catalog). Only Celery transport + results.

---

## Frontend polling audit

**File:** `frontend/app/competitors/page.tsx`

| Mechanism | Interval | Endpoint(s) |
|-----------|----------|---------------|
| Discovery batch poll | 2000 ms | `GET /competitors/discovery-tasks/{id}` |
| Scrape batch poll | 2000 ms | `GET /competitors/scrape-tasks/{id}` |
| Match batch poll | 2000 ms | `GET /competitors/match-tasks/{id}` |
| Workspace refresh during scrape | 5000 ms | `GET .../products?...` (workspace query) |
| Workspace refresh during match | 5000 ms | Same |
| Single listing scrape | 2000 ms loop | `GET /competitor-products/{id}` until `last_seen_at` changes |

### Load model (hundreds of users)

Example: **100** users watching one shared batch scrape:

- Poll traffic: 100 × (0.5 poll/s + 0.2 workspace/s) ≈ **70 req/s** to API (plus tree refresh on completion).
- Each workspace request triggers heavy SQL (`paginate_workspace`).

**WebSocket/SSE readiness:** **Low.** No server push layer; adding SSE would require:

- New FastAPI streaming endpoint or Redis pub/sub subscriber per job channel.
- UI today tightly coupled to `MatchTaskStatus` / `ScrapeTaskStatus` shapes from poll handlers — types in `frontend/lib/types.ts` map 1:1 to poll JSON.

**Migration path (conceptual, not implemented):** Publish Celery `progress_callback` meta to Redis channel `job:{task_id}`; SSE handler in API forwards events; retire 2s poll when connected.

---

## Scraper audit

### Concurrency stack

```
scrape_competitor_products_batch (Celery, 1 worker slot)
  └── asyncio.run(_run_batch_scrape_async)
        └── TechnopolisPlaywrightPool (optional, __aenter__/__aexit__)
        └── per batch of 100 IDs:
              └── asyncio.gather(_scrape_one_listing × N)
                    └── AdaptiveConcurrencyController.acquire()  # 6–20
                          └── fetch_scrape_result_for_listing
                                └── scrape_technopolis_url (OCC → HTTP → pool Playwright)
```

**Files:** `scraping_batch.py`, `adaptive_concurrency.py`, `technopolis_hybrid.py`, `technopolis_playwright_pool.py`

### Playwright pool lifecycle

**`TechnopolisPlaywrightPool`** (`technopolis_playwright_pool.py`):

- `start()`: one Chromium browser, one context, route blocking for images/fonts/trackers.
- Per URL: `new_page()` → work → `page.close()` in `finally` — **good** for page leak prevention.
- `close()`: context + browser + playwright stop on `__aexit__`.

**Batch scope:** Pool lives for entire `run_batch_scrape_async` when `is_technopolis(competitor.domain)` — **good** amortization.

**Single-task path:** `scrape_competitor_product_row` → `asyncio.run` without pool → hybrid may call `scraper._fetch_html_with_page()` → **full browser launch per listing** (`technopolis.py` L165–190). At thousands of single tasks this is the **primary memory/CPU leak risk** (process churn, not necessarily Python refcount leak).

### Memory usage (order of magnitude)

- Chromium: **~200–500 MB** per browser instance.
- Batch: 1 browser + up to `scrape_concurrency_max` (20) concurrent pages — **~GB** per active batch worker.
- `asyncio.gather` on up to **100** tasks per ID batch but semaphore limits concurrent to **20** — still schedules 100 Task objects per loop.

### Retry safety (scraper)

- Playwright: one retry with longer timeout (`_fetch_playwright_with_retry`).
- OCC/HTTP: no Celery-level retry; failed row counted in batch metrics (`ScrapeBatchMetrics.record`).

### Discovery memory

- `collect_product_urls_from_sitemaps` / `DEFAULT_MAX_PRODUCTS = 50_000` caps URL list in memory (`technopolis_full_discovery.py`).
- `full_discovery_batch._dedupe_discovered_urls` holds full deduped list before batched DB check — **~50k URLs** feasible; millions would require streaming-only pipeline (not present).

---

## Matching engine audit

### Algorithm cost

**Core:** `score_product_against_listing()` — O(1) string ops per pair (`services/matching.py`).

**Batch ranking:** `rank_products_for_listing(products, cp)` — O(|products|) per listing.

**Worst case per listing:**

```text
for batch in iter_catalog_batches(500):   # ceil(|catalog|/500) iterations
    rank_products_for_listing(batch)      # scores each product in batch
```

If catalog = **10⁶** products, listings = **10⁶**, no prefilter → **10¹²** score evaluations (infeasible).

**Prefilter path** (`fetch_catalog_candidates_for_listing`): caps at `CANDIDATE_CAP = 2000` merged candidates — then at most 2000 scores per listing — **~2×10⁹** ops at 10⁶ listings without further indexing (still too heavy).

### DB interaction per listing (batch)

1. `SELECT ProductMatch WHERE competitor_product_id = ?` — all rows
2. `DELETE ProductMatch WHERE ... NOT IN (confirmed, rejected)`
3. Optional `INSERT ProductMatch`
4. Commit every 500 listings

**Missing:** bulk skip lookup (e.g. one query for 500 ids → set of skip reasons).

### API path `find_matches_for_listing`

**File:** `routers/competitor_products.py` — `_catalog_for_find_matches` loads **entire catalog into RAM** when no prefilter — **unsafe** beyond ~10⁵ products (worker memory, request timeout).

### Scoring vs persistence thresholds

| Layer | Threshold |
|-------|-----------|
| `matching.py` `THRESHOLD_AUTO` | 95 → `suggested_status` only |
| `match_outcomes.classify_ranked_candidates` | `min_score` default **60** from API |
| Batch API default | `min_score: int = 60` (`MatchAllBody`) |

Inconsistent bands do not break scale but cause extra human review load at scale.

---

## Observability audit

| Signal | Implementation | Scale gap |
|--------|----------------|-----------|
| HTTP access | `DevRequestLogMiddleware` when `debug=True` | No production structured logs |
| Scrape | `scraper_start` / `scraper_success` / `scraper_failure` | No metrics backend |
| Batch | `batch_scrape_progress`, `batch_match_progress` logs | Not correlated with `task_id` in UI |
| Runtime probe | `GET /debug/scrape-runtime` | Manual OCC test |
| DB plans | `scripts/explain_core_queries.py` | Manual |
| Tracing | None | Cannot trace cross API → Celery → scraper |
| Queue depth | None | No visibility into Redis queue length in app |

---

## Fault tolerance summary

| Component | Behavior |
|-----------|----------|
| Postgres | `pool_pre_ping` — reconnect stale connections |
| Celery task failure | Returns `{"error": ...}` dict in match/scrape batch; single scrape returns `"task_error"` string |
| Partial batch | Continues after row errors; commits every N rows |
| Redis down | Broker/backend failure — tasks and polls fail entirely |
| Playwright timeout | Adaptive concurrency may reduce parallel limit |

**No:** circuit breaker for Technopolis, rate limit per domain, graceful degradation mode for UI.

---

## Docker / containerization readiness

**`docker-compose.yml`:**

- Services: `postgres`, `redis`, `backend`, `celery_worker`, `frontend`
- Backend/celery: bind-mount `./backend`, **one** `celery_worker` replica
- Playwright: `--disable-dev-shm-usage` in pool launch args — acknowledges **/dev/shm** limits
- No CPU/memory limits, no worker autoscaling, no separate scrape worker image

**`backend/Dockerfile`:** Installs Chromium in image — suitable for worker nodes with sufficient RAM.

**Horizontal pod scaling:** API stateless **if** DB pool externalized; workers need **anti-affinity** for memory (Playwright). Not defined in repo.

---

## Cloud deployment readiness

| Requirement | Status in repo |
|-------------|----------------|
| External Postgres | Supported via `DATABASE_URL` |
| External Redis | Supported via env |
| Stateless API | Yes |
| Celery workers separate from API | Compose has separate service — good pattern |
| Secrets management | `.env` only — no K8s secrets / IAM |
| Migrations job | Documented `alembic upgrade head` — manual |
| CDN for frontend | Production `next build` — static OK |
| Multi-AZ Redis | Not addressed |
| PgBouncer/RDS proxy | Not addressed |

---

## Recommended production architecture

Target: millions of listings, thousands of jobs, hundreds of users, multi-node workers.

```text
                    ┌─────────────────┐
                    │  CDN / Next.js  │
                    └────────┬────────┘
                             │ HTTPS
                    ┌────────▼────────┐
                    │  API (FastAPI)  │──┐
                    │  + SSE optional │  │
                    └────────┬────────┘  │
                             │          │
              ┌──────────────┼──────────┼──────────────┐
              │              │          │              │
     ┌────────▼────────┐ ┌───▼───┐ ┌────▼────┐ ┌───────▼────────┐
     │ PgBouncer →     │ │ Redis │ │ Redis   │ │ Object store   │
     │ PostgreSQL      │ │broker │ │ pub/sub │ │ (scrape artifacts)│
     └─────────────────┘ └───┬───┘ └─────────┘ └────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
 ┌──────▼──────┐    ┌────────▼────────┐  ┌──────▼──────┐
 │ scrape_queue│    │ match_queue       │  │ discovery_q │
 │ workers     │    │ workers (CPU)     │  │ workers     │
 │ (Playwright │    │ no browser        │  │ (I/O)       │
 │  pool/node) │    │                   │  │             │
 └─────────────┘    └───────────────────┘  └─────────────┘
```

**Code-aligned changes (conceptual):**

1. **Keyset pagination** for workspace + batch iterators — replace `OFFSET` in `paginate_workspace` and `_iter_*_ids`.
2. **Materialized match status** on `competitor_products` (column updated on match confirm/batch) — indexable `status` filter.
3. **Candidate index** — DB-side match candidates (EAN/MFR trigram) instead of Python full scan (`matching_catalog` + `matching_batch`).
4. **Celery queues** — route `scrape_*`, `match_*`, `discovery_*` in `celery_app.conf`; separate worker deployments in Compose/K8s.
5. **Idempotent scrape tasks** — Redis `SET scrape:lock:{cp_id} NX EX` before `scrape_competitor_product`.
6. **Replace `scrape_prices_category` fan-out** with `scrape_competitor_products_batch.delay(category_id=...)` (already exists at competitor router).
7. **SSE** — bridge existing `progress_callback` meta to Redis pub/sub; keep poll as fallback.
8. **Single-listing scrape** — always use `TechnopolisPlaywrightPool` or OCC-only fast path; never `TechnopolisScraper._fetch_html_with_page` per message.
9. **PgBouncer** + explicit `pool_size` on API vs workers.
10. **Result TTL** — `result_expires=3600` on Celery conf.

---

## Priority roadmap (P0–P3)

### P0 — Critical (blocks scale or causes outages)

| # | Item | Evidence |
|---|------|----------|
| 1 | **Eliminate full-catalog match scan** — SQL candidate retrieval + hard cap; never `iter_catalog_batches` for every listing without prefilter | `matching_batch._rank_listing_candidates`, `matching_catalog.iter_catalog_batches` |
| 2 | **Fix `_catalog_for_find_matches` memory** — do not `merged.extend(batch)` for full catalog | `routers/competitor_products.py` L26–33 |
| 3 | **Celery queue separation** — scrape / match / discovery routes + dedicated worker services | `celery_app.py` (no routes) |
| 4 | **Replace category scrape fan-out** with one batch task | `discovery_tasks.scrape_prices_category` L212–221 |
| 5 | **Connection pool limits + PgBouncer** for multi-worker/multi-API | `database.py` |
| 6 | **Idempotent scrape lock** per `competitor_product_id` | No lock today; duplicate enqueue paths |
| 7 | **Keyset pagination** for workspace (and batch ID iterators) | `workspace_query.paginate_workspace`, `matching_batch._iter_competitor_product_ids` |

### P1 — Important (10⁵–10⁶ listings, tens of workers)

| # | Item | Evidence |
|---|------|----------|
| 1 | Bulk `_should_skip` for 500 ids per query | `matching_batch._should_skip_competitor_product` |
| 2 | Materialized / denormalized `match_status` on `competitor_products` | `_effective_status_expr` computed join |
| 3 | Partial indexes for scrape batch filters (`only_stale`, `only_missing`) | `scraping_batch._scrape_ids_stmt` |
| 4 | Celery `result_expires`, task time limits, `acks_late` | No config in `celery_app.py` |
| 5 | **SSE or Redis pub/sub** for job progress; reduce 2s poll | `competitors/page.tsx` useEffects |
| 6 | Single-listing scrape uses shared pool or OCC-only | `technopolis.py` `_fetch_html_with_page` |
| 7 | `COUNT(*)` avoidance — approximate counts or cached totals for workspace | `paginate_workspace` L225–226 |
| 8 | Autoretry with backoff for transient scrape failures (not Playwright timeout storms) | `scrape_tasks.py` |

### P2 — Improvements (operations & cost)

| # | Item | Evidence |
|---|------|----------|
| 1 | Redis broker vs result DB index split | `config.effective_celery_*` |
| 2 | Archive/partition `price_snapshots` if history enabled | `price_snapshot.py` |
| 3 | Cap JSONB `top_candidates` size; store only top 3 at scale | `ProductMatch.top_candidates` |
| 4 | `explain_core_queries` in CI on seed data | `scripts/explain_core_queries.py` |
| 5 | Prometheus metrics from `ScrapeBatchMetrics` / match stats | `scraping_batch.ScrapeBatchMetrics` |
| 6 | Rate limit Technopolis domain per worker | `AdaptiveConcurrencyController` only handles timeouts |
| 7 | Discovery streaming without 50k in-memory list | `DEFAULT_MAX_PRODUCTS` |

### P3 — Nice-to-have

| # | Item |
|---|------|
| 1 | Read replicas for workspace read path |
| 2 | CDN cache for `GET /competitors/tree` with ETag |
| 3 | Dedicated “analytics” worker for `explain_core_queries` style reports |
| 4 | Multi-tenant `tenant_id` partitioning on `products` / listings |

---

## Quick reference — scalability-critical symbols

| Concern | Symbol | File |
|---------|--------|------|
| Workspace page SQL | `paginate_workspace` | `services/workspace_query.py` |
| Best match window | `best_match_subquery` | `db/latest_price.py` |
| Batch scrape | `run_batch_scrape_competitor_products` | `services/scraping_batch.py` |
| Playwright pool | `TechnopolisPlaywrightPool` | `scrapers/sites/technopolis_playwright_pool.py` |
| Adaptive limit | `AdaptiveConcurrencyController` | `services/adaptive_concurrency.py` |
| Batch match | `run_batch_match_competitor_products` | `services/matching_batch.py` |
| Catalog prefilter | `fetch_catalog_candidates_for_listing` | `services/matching_catalog.py` |
| Celery app | `celery_app` | `app/celery_app.py` |
| Task poll API | `get_scrape_task_status`, `get_match_task_status` | `routers/competitors.py` |
| UI polling | `useEffect` intervals 2000/5000 ms | `frontend/app/competitors/page.tsx` |
| Page limit cap | `MAX_PAGE_LIMIT = 100` | `db/pagination.py` |

---

*End of scalability audit.*
