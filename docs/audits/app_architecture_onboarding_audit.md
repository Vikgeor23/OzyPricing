# App Architecture & Onboarding Audit

**Project:** Pricing Monitor (competitor pricing / scraper MVP)  
**Audit date:** 2026-05-22  
**Mode:** Read-only (no code changes)

---

## Executive summary

Pricing Monitor is a **three-tier local/SaaS skeleton**: a **Next.js 14** App Router UI talks to a **FastAPI** API over HTTP (`NEXT_PUBLIC_API_URL`), which reads/writes **PostgreSQL** via **SQLAlchemy 2.x (sync)** and enqueues long work to **Celery** workers through **Redis** (broker + result backend). The dominant retailer integration is **Technopolis Bulgaria** (`technopolis.bg`): category trees, sitemap/full-domain URL discovery, hybrid price scraping (OCC API → optional HTTP → Playwright pool), and deterministic catalog matching with manual confirm/reject.

**What works end-to-end today**

1. **Own catalog** — XLSX import via `POST /products/import-xlsx` (synchronous); Products page shows paginated price comparison.
2. **Competitors workspace** — Tree + paginated listing table (`GET /competitors/tree`, workspace endpoints) with batch discover/scrape/match and **poll-based** live progress for **competitor-scoped** Celery tasks.
3. **Matching** — `score_product_against_listing()` + batch `run_batch_match_competitor_products()` persist `ProductMatch` rows; UI filters by status and supports `POST /matches/confirm` and `POST /matches/reject`.
4. **Scraping** — Single-listing and batch scrape update `competitor_products.latest_*` (history off by default via `PRICE_HISTORY_ENABLED=false`).

**Main gaps / risks**

| Area | Risk |
|------|------|
| API URL / prefix | Production expects `NEXT_PUBLIC_API_URL=…/api`; dev defaults to `http://localhost:8000` without `/api`. Backend mirrors both; frontend must match deployment. |
| Live progress | Only **batch** discover/scrape/match at competitor scope poll Celery; category jobs (`discover-products`, `scrape-prices`, `find-matches`) have **no task polling UI**. |
| Status vocabulary | Scoring `suggested_status` (`auto_match`, `weak_match`) ≠ persisted `ProductMatch.status` (`auto_matched`, `low_confidence`, …). |
| Threshold drift | `matching.py` review band at **80** vs batch `classify_ranked_candidates` min_score default **60**. |
| Frontend data shape | `CompetitorsPage` loads catalog via `GET /products` typed as `Product[]` but API returns `{ rows, total, … }`. |
| Docker frontend | Compose builds **production** Next image (`npm run build`); env baked at build — rebuild required after `NEXT_PUBLIC_API_URL` changes. README mentions dev bind-mount/`frontend_node_modules` not present in current `docker-compose.yml`. |
| Celery reload | Worker does not auto-reload; task/scraper edits need `docker compose restart celery_worker`. |

---

## Current architecture map

```mermaid
flowchart TB
  subgraph browser [Browser]
    UI[Next.js App Router]
  end

  subgraph api [FastAPI - app/main.py]
    R[routers/*]
    S[services/*]
  end

  subgraph data [Data]
    PG[(PostgreSQL)]
    RD[(Redis)]
  end

  subgraph worker [Celery - app/celery_app.py]
    T[app/tasks/*]
    SC[app/scrapers/*]
  end

  UI -->|fetch NEXT_PUBLIC_API_URL| R
  R --> S
  S --> PG
  R -->|AsyncResult.delay| RD
  RD --> T
  T --> S
  T --> SC
  T --> PG
  T -->|update_state PROGRESS| RD
  UI -->|poll GET .../tasks/{id}| R
```

### Repository layout (source only)

```text
/home/varox/Pricing-Monitor/
├── docker-compose.yml          # postgres, redis, backend, celery_worker, frontend
├── .env.example                # NEXT_PUBLIC_API_URL (+ production /api note)
├── README.md
├── backend/
│   ├── Dockerfile              # Python 3.12 + Playwright chromium
│   ├── requirements.txt
│   ├── alembic/                # migrations (versions 0001–0012)
│   ├── app/
│   │   ├── main.py             # FastAPI create_app(), /api mirror
│   │   ├── config.py           # Settings (Pydantic)
│   │   ├── database.py         # engine, SessionLocal, get_db
│   │   ├── celery_app.py
│   │   ├── middleware/request_log.py
│   │   ├── models/             # ORM
│   │   ├── schemas/            # Pydantic API models
│   │   ├── routers/            # HTTP endpoints
│   │   ├── services/           # domain + batch logic
│   │   ├── tasks/              # Celery entrypoints
│   │   ├── scrapers/           # site adapters
│   │   └── db/                 # SQL helpers (pagination, latest_price)
│   ├── scripts/                # CLI audits / EXPLAIN
│   └── tests/
├── frontend/
│   ├── Dockerfile              # next build + start (prod)
│   ├── app/                    # routes (pages)
│   ├── components/             # SidebarLayout, ApiHealthStatus
│   ├── contexts/ApiHealthContext.tsx
│   └── lib/                    # config.ts, api.ts, types.ts
└── docs/audits/                # prior Technopolis audits + this doc
```

### Runtime entry points

| Layer | Entry | Command / notes |
|-------|--------|-----------------|
| API | `app.main:app` | `uvicorn app.main:app` (Compose: `--reload`) |
| Worker | `app.celery_app` | `celery -A app.celery_app worker --loglevel=info` |
| Frontend | `frontend/app/layout.tsx` | `npm run dev` (host) or `npm run start` (Docker prod image) |
| Migrations | `alembic/env.py` | `alembic upgrade head` |

---

## End-to-end flows

### 1. Catalog onboarding (own products)

```text
Integrations UI (/integrations)
  → download GET /products/template-xlsx
  → uploadXlsxImport POST /products/import-xlsx
       → product_import.import_products_from_xlsx()
            → upsert by sku (product_service)
  → optional list GET /products?limit=100
Products UI (/products)
  → GET /products/price-comparison?limit=&offset=
       → price_comparison_service.build_price_comparison_page()
```

### 2. Competitor setup & discovery

```text
Competitors UI (/competitors)
  → GET /competitors/tree → competitor_tree_service.build_competitor_forest()
  → POST /competitors/{id}/discover-categories
       → discover_categories_competitor (Technopolis only)
  → POST /competitors/{id}/discover-all-product-urls
       → discover_all_product_urls_for_competitor (sitemap incremental)
       → poll GET /competitors/discovery-tasks/{task_id}
  → POST /competitor-categories/{cat}/discover-products
       → discover_products_category (no poll UI; 90s table probe)
```

### 3. Scraping

```text
Single URL:
  POST /competitor-products (scrape_after_create) or POST /jobs/scrape-product/{id}
    → scrape_competitor_product.delay()
    → scrape_competitor_product_by_id() → fetch_scrape_result_for_listing()
    → apply_scrape_result_to_listing() → latest_* + optional PriceSnapshot

Batch:
  POST /competitors/{id}/scrape-all
    → scrape_competitor_products_batch (PROGRESS meta)
    → poll GET /competitors/scrape-tasks/{task_id}
    → workspace table refresh every 5s while running

Category (legacy path):
  POST /competitor-categories/{id}/scrape-prices
    → scrape_prices_category → N × scrape_competitor_product.delay (no aggregate task status)
```

### 4. Matching & manual review

```text
Batch:
  POST /competitors/{id}/match-all
    → match_competitor_products_batch
    → poll GET /competitors/match-tasks/{task_id}

Per row (sync API):
  POST /competitor-products/{id}/find-matches
    → rank_products_for_listing() (top 5)

Manual:
  POST /matches/confirm → match_service.upsert_match_and_link_product() + cp.product_id
  POST /matches/reject  → status rejected, may clear cp.product_id
```

---

## Frontend audit

### API configuration

| File | Symbol | Behavior |
|------|--------|----------|
| `frontend/lib/config.ts` | `API_BASE_URL` | From `NEXT_PUBLIC_API_URL`, default `http://localhost:8000`; warns if host is `backend` |
| `frontend/lib/api.ts` | `api`, `checkApiHealth`, `uploadXlsxImport` | `fetch` with `mode: "cors"`, `credentials: "omit"`; GET retry on network failure; `ApiError` / `ApiAbortError` |
| `.env.example` (root) | — | Documents production `https://…/api` |

**Important:** Paths in the UI are **unprefixed** (`/competitors/tree`, not `/api/competitors/tree`). That works when `API_BASE_URL` already includes `/api` (production) or when hitting uvicorn directly (local `:8000`). `backend/app/main.py` registers **duplicate** routes under `API_MOUNT_PREFIX = "/api"` for proxy deployments.

### Layout & navigation

| File | Role |
|------|------|
| `frontend/app/layout.tsx` | Root layout, wraps `SidebarLayout` |
| `frontend/components/SidebarLayout.tsx` | Nav: `/integrations`, `/competitors`, `/products` |
| `frontend/contexts/ApiHealthContext.tsx` | `checkApiHealth()` → `GET /health` |
| `frontend/components/ApiHealthStatus.tsx` | Sidebar connectivity widget |

### Routes (pages)

| Route | File | Purpose |
|-------|------|---------|
| `/` | `app/page.tsx` | Redirect → `/products` |
| `/products` | `app/products/page.tsx` | Price comparison table |
| `/integrations` | `app/integrations/page.tsx` | XLSX import + template link |
| `/competitors` | `app/competitors/page.tsx` | **Primary** competitor workspace (tree, table, jobs, matching UI) |
| `/dashboard`, `/price-monitor`, `/products/import`, `/scrape-jobs` | `app/*/page.tsx` | Redirects to products/competitors/integrations |

### Main UI surface: `CompetitorsPage`

**File:** `frontend/app/competitors/page.tsx` (large client component)

**State & helpers (representative):**

- Workspace pagination: `fetchWorkspacePage`, `workspaceProductsQuery`, filters `WorkspaceScrapeFilter`, `WorkspaceMatchFilter`
- Tree: `refreshTree` → `GET /competitors/tree`
- Batch jobs: `discoverAllProductUrls`, `scrapeAllProducts`, `matchAllProducts`
- Polling `useEffect` hooks (2s): `discoveryAllTaskId`, `scrapeAllTaskId`, `matchAllTaskId`
- Incremental table refresh (5s) during active scrape/match batch
- Row actions: `scrapeListing`, `runFindMatches`, `confirmMatch`, `rejectMatch`, `openChooser`
- Category batch: `enqueueDiscovery`, `discoverProductsForCategory` (2s probe, not Celery poll)

**API calls used (non-exhaustive):**

| UI action | HTTP |
|-----------|------|
| Tree | `GET /competitors/tree` |
| Workspace (category) | `GET /competitor-categories/{id}/products?...` |
| Workspace (all products) | `GET /competitors/{id}/products?...` |
| Add URL | `POST /competitor-products` |
| Discover all | `POST /competitors/{id}/discover-all-product-urls` + poll `GET /competitors/discovery-tasks/{id}` |
| Scrape all | `POST /competitors/{id}/scrape-all` + poll `GET /competitors/scrape-tasks/{id}` |
| Match all | `POST /competitors/{id}/match-all` + poll `GET /competitors/match-tasks/{id}` |
| Category discover/scrape/match | `POST /competitor-categories/{id}/discover-products|scrape-prices|find-matches` |
| Single scrape | `POST /jobs/scrape-product/{id}` then poll `GET /competitor-products/{id}` for `last_seen_at` |
| Find matches | `POST /competitor-products/{id}/find-matches` |
| Confirm/reject | `POST /matches/confirm`, `POST /matches/reject` |

**Types:** `frontend/lib/types.ts` — `CategoryWorkspaceProduct`, `MatchTaskStatus`, `ScrapeTaskStatus`, `DiscoveryTaskStatus`, etc.

### Other frontend pages

- **`ProductsComparisonPage`** (`app/products/page.tsx`): `GET /products/price-comparison`
- **`IntegrationsPage`**: correctly uses `{ rows: Product[] }` for `GET /products?limit=100`

### Frontend ↔ backend mismatch (confirmed)

1. **`openChooser` catalog load** (`competitors/page.tsx` ~797): `api.get<Product[]>("/products")` — backend returns `ProductListPage` with `rows`, not a bare array.
2. **`CompetitorProduct` type** in `types.ts` omits `latest_*` fields exposed in workspace DTOs.
3. **Production API prefix**: must set `NEXT_PUBLIC_API_URL` to include `/api` when behind Cloudflare; no runtime prefix in `api.ts`.

---

## Backend audit

### Application startup

**`backend/app/main.py`**

- `create_app()` loads `Settings` via `get_settings()`, optional `DEBUG` logging
- `CORSMiddleware` from `cors_origins` (comma-separated)
- `DevRequestLogMiddleware` when `debug=True` (`app/middleware/request_log.py`)
- Routers included twice: unprefixed + `prefix="/api"`
- Health: `health()` at `/health` and `/api/health`

**Routers** (`APP_ROUTERS`):

| Module | Prefix | Tag |
|--------|--------|-----|
| `routers/matches.py` | `/matches` | matches |
| `routers/products.py` | `/products` | products |
| `routers/competitors.py` | `/competitors` | competitors |
| `routers/competitor_categories.py` | `/competitor-categories` | competitor-categories |
| `routers/competitor_products.py` | `/competitor-products` | competitor-products |
| `routers/prices.py` | `/price-snapshots` | (prices) |
| `routers/jobs.py` | `/jobs` | jobs |
| `routers/dashboard.py` | `/dashboard` | dashboard |
| `routers/debug.py` | `/debug` | debug |

### DB session

**`backend/app/database.py`**

- `create_engine(database_url, pool_pre_ping=True)`
- `SessionLocal` + `get_db()` generator for FastAPI `Depends`
- Celery tasks use `SessionLocal()` directly (no request scope)

### Configuration

**`backend/app/config.py` — `Settings`**

Notable env-driven fields: `database_url`, `redis_url`, `celery_broker_url`, `celery_result_backend`, `cors_origins`, `price_history_enabled`, full `scrape_*` tuning block (concurrency, OCC, timeouts, skip dead URLs, progress interval, batch commit size).

### Services (domain map)

| Service | Responsibility |
|---------|----------------|
| `product_service` | Product CRUD, list pages |
| `product_import` | XLSX parse/import |
| `competitor_service` | Competitor CRUD |
| `competitor_product_service` | Listing CRUD, URL upsert |
| `competitor_category_service` | Category tree replace, product upsert from discovery |
| `competitor_tree_service` | Forest for UI tree |
| `workspace_query` | Paginated workspace SQL + filters |
| `workspace_match_fields` | Parse `top_candidates` JSON for API |
| `matching` | `score_product_against_listing`, `rank_products_for_listing` |
| `matching_catalog` | Prefilter candidates by EAN/MFR/brand |
| `match_outcomes` | `classify_ranked_candidates`, `MatchPersistPlan` |
| `matching_batch` | `run_batch_match_competitor_products`, skip rules |
| `match_service` | Confirm/reject |
| `scraping_batch` | Concurrent batch scrape + progress |
| `scrape_fetch` / `scrape_persist` | Fetch layer + DB persist |
| `full_discovery_batch` | Sitemap incremental URL import |
| `price_comparison_service` | Products page rows |
| `competitor_overview_service` | Legacy overview list |
| `listing_price` | Read `latest_*` as effective price |
| `url_health` | Dead URL / timeout counters |

### Scrapers

**`backend/app/scrapers/registry.py` — `get_scraper_for_domain()`**

- `technopolis.bg` → `TechnopolisScraper`
- Else → `DemoScraper` (synthetic)

**Technopolis stack (files under `scrapers/sites/`):**

- `technopolis_hybrid.py` — `scrape_technopolis_url()` (OCC, HTTP, Playwright)
- `technopolis_occ_api.py` — OCC REST
- `technopolis_playwright_pool.py` — shared browser pool for batch
- `technopolis_categories.py`, `technopolis_discovery.py`, `technopolis_full_discovery.py` — discovery
- `technopolis_specs.py`, `technopolis_js_extract.py`, etc.

---

## Worker / Celery audit

### Application

**`backend/app/celery_app.py`**

```python
celery_app = Celery(
    "price_monitor",
    broker=settings.effective_celery_broker,
    backend=settings.effective_celery_backend,
    include=[
        "app.tasks.scrape_tasks",
        "app.tasks.scrape_batch_tasks",
        "app.tasks.discovery_tasks",
        "app.tasks.match_tasks",
    ],
)
```

- **Queues:** not customized — default Celery queue only
- **Serialization:** JSON
- **Result backend:** same Redis URL as broker (Compose `CELERY_RESULT_BACKEND`)

### Tasks

| Task | Module | `bind` | PROGRESS | Notes |
|------|--------|--------|----------|-------|
| `scrape_competitor_product` | `scrape_tasks.py` | no | no | Single listing |
| `scrape_competitor_products_batch` | `scrape_batch_tasks.py` | yes | yes | `run_batch_scrape_competitor_products` |
| `discover_categories_competitor` | `discovery_tasks.py` | no | no | Technopolis categories |
| `discover_products_category` | `discovery_tasks.py` | no | no | PLP URL harvest |
| `discover_all_product_urls_for_competitor` | `discovery_tasks.py` | yes | yes | `run_incremental_full_discovery` |
| `scrape_prices_category` | `discovery_tasks.py` | no | no | Fans out N single scrapes |
| `find_matches_category` | `discovery_tasks.py` | no | no | Sync `apply_best_matches_for_category` |
| `match_competitor_products_batch` | `match_tasks.py` | yes | yes | `run_batch_match_competitor_products` |

### Job creation & tracking

1. **Enqueue:** Router calls `task.delay(...)` → returns `task_id` in response (`DiscoverQueued`, `ScrapeAllQueued`, `MatchAllQueued`, etc.).
2. **Progress:** Bound tasks call `self.update_state(state="PROGRESS", meta=meta)` from `progress_callback` in batch services.
3. **Poll:** `competitors.py` uses `celery.result.AsyncResult(task_id, app=celery_app)`:
   - `get_discovery_task_status`
   - `get_scrape_task_status` → `scrape_task_status_from_meta`
   - `get_match_task_status` → `match_task_status_from_meta`
4. **Completion:** When `ready`, merges `async_result.result` dict into meta for final counts.

**Log markers:** `batch_scrape_*`, `batch_match_*`, `category_discovery_*`, `scraper_start` / `scraper_success` / `scraper_failure` (see README).

---

## Database audit

### Tables / models

| Model | Table | File |
|-------|-------|------|
| `Product` | `products` | `models/product.py` |
| `Competitor` | `competitors` | `models/competitor.py` |
| `CompetitorCategory` | `competitor_categories` | `models/competitor_category.py` |
| `CompetitorProduct` | `competitor_products` | `models/competitor_product.py` |
| `ProductMatch` | `product_matches` | `models/product_match.py` |
| `PriceSnapshot` | `price_snapshots` | `models/price_snapshot.py` |

### Relationships (logical)

```text
Competitor 1──* CompetitorCategory (parent/children self-FK)
Competitor 1──* CompetitorProduct
CompetitorCategory 0──* CompetitorProduct (nullable FK, SET NULL on delete)
Product 0──* CompetitorProduct (optional direct link via product_id)
Product 1──* ProductMatch *──1 CompetitorProduct
CompetitorProduct 1──* PriceSnapshot
```

**Uniqueness:**

- `competitor_products`: `(competitor_id, url)`
- `product_matches`: `(product_id, competitor_product_id)`

### Key columns for operations

**`competitor_products` (listing hub):**

- Identity: `url`, `title`, `ean`, `manufacturer_code`, `model`, `specs_json`, `technopolis_product_code`
- Price cache: `latest_price`, `latest_promo_price`, `latest_scraped_at`, `latest_scrape_status`, `latest_scrape_error_code`
- Health: `is_dead`, `consecutive_timeout_count`, `consecutive_not_found_count`
- Discovery: `discovered_at`, `discovery_source`, `competitor_category_id`

**`product_matches` (matching hub):**

- `match_score`, `match_method`, `status`, `match_reason`, `match_warnings` (JSONB)
- `candidate_count`, `top_candidates` (JSONB), `matched_by`

### Migrations (Alembic)

| Revision | File | Theme |
|----------|------|--------|
| `20240520_0001` | `initial.py` | Core tables |
| `20240521_0002` | | `manufacturer_code` on products |
| `20260522_0003` | | `competitor_categories` |
| `20260520_0004` | | Workspace pagination indexes |
| `20260520_0005` | | Product matching fields |
| `20260520_0006` | | Performance indexes + `pg_trgm` |
| `20260521_0007` | | `latest_*` on competitor_products |
| `20260521_0008`–`0010` | | Discovery metadata, scrape error code, URL health |
| `20260522_0011` | | Match metadata columns |
| `20260523_0012` | | Metadata repair |

**Apply:** `docker compose run --rm backend alembic upgrade head`

### SQL helpers

**`backend/app/db/latest_price.py`**

- `latest_price_subquery()` — window over `price_snapshots` (used when history enabled / legacy)
- `best_match_subquery()` — best `ProductMatch` per listing (status rank, then score)
- `load_latest_price_map()` — batch map for comparison page

**`backend/app/db/pagination.py`** — `DEFAULT_PAGE_LIMIT` 75, `MAX_PAGE_LIMIT` 100

---

## Matching flow audit

### 1. Own products upload/import

- **Endpoint:** `POST /products/import-xlsx` → `import_products_from_xlsx()` in `services/product_import.py`
- **Rules:** `sku` + `name` required; upsert by `sku`; row-level errors in `ProductImportSummary`
- **UI:** `IntegrationsPage` + `uploadXlsxImport()`

### 2. Competitor product discovery

| Mode | Trigger | Worker | Output |
|------|---------|--------|--------|
| Category tree | `POST /competitors/{id}/discover-categories` | `discover_categories_competitor` | `CompetitorCategory` tree |
| Category PLP | `POST /competitor-categories/{id}/discover-products` | `discover_products_category` | New `CompetitorProduct` rows |
| Full domain | `POST /competitors/{id}/discover-all-product-urls` | `discover_all_product_urls_for_competitor` | Batched upsert + category paths |

### 3. Matching algorithm

**Core:** `services/matching.py` — `score_product_against_listing(product, cp)`

Priority of signals (highest first):

1. EAN exact → score 100, method `ean_exact`
2. Manufacturer code exact → 95
3. SKU in title/specs → 92
4. Model exact / in specs → 91/90
5. Brand + code/model → 90/88
6. Brand + fuzzy name (`SequenceMatcher`) → 60–88
7. Title similarity → 60–79
8. Token overlap → 40–59
9. Else → 0 `no_signal`

**Attribute adjustments:** storage/color/memory compare catalog vs `specs_json` (+/− score, warnings).

**Suggested bands** (`matching.py` constants — UI hints only):

- `THRESHOLD_AUTO = 95` → `auto_match`
- `THRESHOLD_REVIEW = 80` → `needs_review`
- `THRESHOLD_WEAK = 60` → `weak_match`

### 4. Batch score → persist

**Candidate search:** `matching_batch._rank_listing_candidates()`

- Try `fetch_catalog_candidates_for_listing()` (EAN, MFR, brand/model, SKU)
- Else full catalog in batches via `iter_catalog_batches()`
- Rank with `rank_products_for_listing(..., limit=5, min_score=1)`

**Classification:** `match_outcomes.classify_ranked_candidates(ranked, min_score=60)` (default from API)

| Condition | Persisted `status` | `persist` |
|-----------|-------------------|-----------|
| No ranked rows | `no_candidate` | false |
| ≥2 within 5 points of top | `needs_review`, `matched_by=multiple_candidates` | true |
| Top ≥ 95 | `auto_matched` | true |
| Top ≥ min_score (60) | `needs_review` | true |
| Top ≥ 1, &lt; min_score | `low_confidence` | true |
| Else | `no_candidate` | false |

**Skip rules** (`_should_skip_competitor_product`):

- Existing `confirmed` / `rejected` match
- `only_unmatched=True` and any existing match row → `already_matched`

**Persist:** Deletes non-terminal matches for listing, inserts new `ProductMatch` — does **not** set `CompetitorProduct.product_id` until user confirms.

### 5. Manual review

- **Workspace** shows `match_status`, `match_reason`, `top_candidates`
- **Actions:** `confirmMatch` / `rejectMatch` → `match_service`
- **Sync candidate picker:** `POST /competitor-products/{id}/find-matches` (can scan **entire** catalog if no prefilter — expensive)

### 6. Where live progress should come from

| Operation | Progress source |
|-----------|-----------------|
| Match all (competitor) | Celery `PROGRESS` → `GET /competitors/match-tasks/{id}` |
| Scrape all | `GET /competitors/scrape-tasks/{id}` |
| Discover all URLs | `GET /competitors/discovery-tasks/{id}` |
| Category find-matches | **No task id exposed** — only logs + eventual DB change |
| Category scrape-prices | **N separate tasks** — no aggregate endpoint |
| Single scrape | UI polls listing `last_seen_at` (updated in `scrape_persist`) |

---

## Scraping flow audit

### Single URL

1. `scrape_competitor_product` task
2. `scrape_competitor_product_by_id()` → `fetch_scrape_result_for_listing()`
3. Technopolis: `scrape_technopolis_url()` (OCC if `scrape_occ_enabled`, else HTTP/Playwright per config)
4. `apply_scrape_result_to_listing()` updates `latest_*`, `last_seen_at`, optional `PriceSnapshot` if `price_history_enabled`
5. `url_health.update_url_health_after_scrape()`, optional breadcrumb category path

### Batch scrape

**`services/scraping_batch.run_batch_scrape_competitor_products()`**

- Selects IDs in batches (`CP_BATCH_SIZE = 100`)
- Filters: `only_missing`, `only_stale`, `skip_recent_failures`, `skip_dead_urls`
- Async concurrent scrape with `AdaptiveConcurrencyController` + shared `TechnopolisPlaywrightPool`
- Commits every `scrape_batch_commit_size` (default 20)
- Progress every `scrape_progress_interval_sec` (default 3s) → Celery meta + rich metrics (`ScrapeBatchMetrics`)

### Discovery (no price)

- **Categories:** Playwright navigation → `replace_category_tree()`
- **Category products:** `discover_product_urls_for_category()` → `upsert_discovered_products()`
- **Full domain:** sitemap pipeline in `full_discovery_batch` / `technopolis_full_discovery`

### Price extraction

- Primary: OCC API (`scrape_technopolis_occ`)
- Fallback: HTTP HTML parse or Playwright (`technopolis_hybrid`)
- Demo scraper: random placeholder prices for unknown domains

### Job progress / logs

- **API poll schemas:** `schemas/scrape_batch.py` — `ScrapeTaskStatus`, `scrape_task_status_from_meta`
- **Debug:** `GET /debug/scrape-runtime` — OCC probe + config flags (`routers/debug.py`)
- **Failure artifacts:** `backend/storage/scrape_failures/` (screenshots per README)

---

## Observability / logging audit

| Mechanism | Location | When |
|-----------|----------|------|
| Request log | `DevRequestLogMiddleware` | `DEBUG=true`: method, path, Origin |
| App startup CORS log | `main.create_app` | Logs allowed origins |
| Celery/task logs | `tasks/*.py`, `scraping_batch`, `matching_batch` | `batch_*_progress`, failures |
| Scraper markers | Technopolis / `scrape_fetch` | `scraper_start`, `scraper_success`, `scraper_failure` |
| Health | `GET /health` | No DB |
| Frontend | `ApiHealthContext` | Sidebar reachability |

**Gaps:** No centralized log aggregation, no OpenTelemetry, no WebSocket/SSE for jobs (poll-only), no structured audit trail for manual match decisions beyond DB rows.

---

## Risks and gaps

### Frontend / backend contract

- `GET /products` response shape vs `CompetitorsPage` catalog loader
- `NEXT_PUBLIC_API_URL` must match deployment path (`/api` vs none)
- `CompetitorProductRead` lacks `latest_*` — row scrape polling uses `last_seen_at` only

### CORS / config

- Separate origins for `localhost` vs `127.0.0.1` — must list both in `CORS_ORIGINS`
- `credentials: "omit"` / `allow_credentials=False` — consistent today; breaking if cookies added later

### Docker / stale builds

- Frontend image runs `npm run build` — **rebuild** after env changes
- Backend/celery bind-mount `./backend` but **Playwright/deps** need image rebuild on `requirements.txt` change
- README dev frontend volume story **does not match** current `docker-compose.yml` (no frontend bind mount)

### Database / performance

- `find-matches` without prefilter loads **full catalog** into memory on API worker
- Batch match with large catalog + no EAN can be CPU-heavy (`iter_catalog_batches` + scoring)
- Workspace query is optimized with indexes (`0004`, `0006`) and `best_match_subquery` — still depends on migration head
- `GET /competitor-products/overview` counts all listings for total (full table count)

### Live progress gaps

- Category-level Celery jobs: no `task_id` returned to UI (DiscoverQueued only for some)
- `scrape_prices_category`: no progress bar (N async single tasks)
- Single scrape: no `GET` task status poll (only listing field poll)
- Category discover uses **total row count** probe, not Celery meta

### Matching status inconsistencies

| Concept | Values |
|---------|--------|
| `MatchEvaluation.suggested_status` | `auto_match`, `needs_review`, `weak_match`, `no_match` |
| `ProductMatch.status` (persisted) | `auto_matched`, `needs_review`, `low_confidence`, `no_candidate`, `confirmed`, `rejected` |
| Workspace filter UI | includes `no_match` label mapping to `no_candidate` |
| `matching.py` review threshold | 80 (suggested) vs batch min 60 (persist) |
| `find_matches_category` | `only_unmatched=False` (re-runs all listings in category) |

### Other

- No authentication / multi-tenant enforcement (`tenant_id` on Product unused in routers)
- `DemoScraper` for non-Technopolis competitors can mask integration gaps
- Celery default queue — heavy scrape + match share one worker pool

---

## Recommended implementation plan

### P0 — Critical fixes

1. **Fix `CompetitorsPage` catalog fetch** — use `GET /products?limit=…` and read `.rows` (same as Integrations).
2. **Document and enforce `NEXT_PUBLIC_API_URL`** for each environment (local vs `…/api` production); add smoke test in CI that hits `/health` with configured base URL.
3. **Ensure migrations at head** on deploy (`alembic upgrade head`) — workspace/match metadata breaks without `0011`/`0012`.
4. **Category `scrape-prices` observability** — either return a batch task id or document that only per-row scrape applies; avoid silent “queued N jobs” with no UI feedback.

### P1 — Important fixes

1. **Unify match status vocabulary** — map `suggested_status` → persisted status in one module; align UI badges and API filters.
2. **Align review thresholds** — single config for `min_score`, auto-match floor (95), and “weak” band; document in API schema.
3. **Poll or progress for category jobs** — return `task_id` from category discover/match/scrape endpoints and reuse discovery/scrape/match poll routes (or dedicated category task route).
4. **`find-matches` performance cap** — never load full catalog on request path; require prefilter or DB-side candidate query.
5. **Docker Compose dev story** — either add frontend dev service with bind mount + `npm run dev` or update README to match prod frontend container.
6. **Rebuild checklist** in README when `NEXT_PUBLIC_API_URL` or backend deps change.

### P2 — Improvements

1. **Expose `latest_*` on `CompetitorProductRead`** for consistent row-level scrape feedback.
2. **WebSocket or SSE** optional channel for batch job meta (reduce 2s polling load).
3. **Separate Celery queues** — `scrape`, `match`, `discovery` with concurrency limits.
4. **Structured logging** (JSON) with `task_id`, `competitor_id`, `competitor_product_id`.
5. **Price history toggle** documented per environment; migration path when enabling `PRICE_HISTORY_ENABLED`.
6. **Auth + tenant scoping** when moving beyond MVP.

### P3 — Nice-to-have

1. Consolidate legacy redirects into middleware config only.
2. Admin page for failed scrape artifacts in `storage/scrape_failures/`.
3. Export workspace CSV async job.
4. Match explanation UI from `top_candidates` without re-running find-matches.
5. GraphQL or BFF if API surface keeps growing.

---

## Questions / unknowns

1. **Production routing:** Is Cloudflare stripping `/api` before uvicorn, or must the browser call `https://host/api/...`? Current code supports both if `NEXT_PUBLIC_API_URL` is set correctly — confirm live ingress rules.
2. **Worker count:** How many `celery_worker` replicas run in production? Single worker bottlenecks batch scrape + match.
3. **`find_matches_category` vs `match-all`:** Is intentional that category match uses `only_unmatched=False` while competitor match-all defaults `only_unmatched=True`?
4. **Auto-link policy:** Should `auto_matched` rows set `CompetitorProduct.product_id` automatically, or always require confirm? Current code only links on confirm.
5. **Non-Technopolis competitors:** Product roadmap for additional scrapers vs DemoScraper placeholder.
6. **`tenant_id`:** Planned multi-tenant model or legacy field?
7. **Frontend Compose intent:** Was hot-reload frontend removed intentionally? README still describes `frontend_node_modules` volume.
8. **Match repair migration `0012`:** What production issue did it fix — need runbook for existing DBs partial on `0011`?

---

## Quick reference — key symbols

| Area | Symbol | Path |
|------|--------|------|
| FastAPI factory | `create_app` | `backend/app/main.py` |
| API prefix | `API_MOUNT_PREFIX` | `backend/app/main.py` |
| Celery app | `celery_app` | `backend/app/celery_app.py` |
| Batch match | `run_batch_match_competitor_products` | `backend/app/services/matching_batch.py` |
| Score fn | `score_product_against_listing` | `backend/app/services/matching.py` |
| Batch scrape | `run_batch_scrape_competitor_products` | `backend/app/services/scraping_batch.py` |
| Workspace page | `list_category_workspace_page` | `backend/app/services/workspace_query.py` |
| Main UI | `CompetitorsPage` (default export) | `frontend/app/competitors/page.tsx` |
| HTTP client | `api`, `API_BASE_URL` | `frontend/lib/api.ts`, `frontend/lib/config.ts` |

---

*End of audit.*
