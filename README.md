# Price Monitor (standalone SaaS skeleton)

Competitor price monitoring MVP: **Next.js** UI, **FastAPI** API, **PostgreSQL**, **Redis**, **Celery** workers, **SQLAlchemy** models, **Alembic** migrations, and **Docker Compose** for local development.

This repository is intentionally **not** wired to Varox — it is a separate greenfield app.

## Stack

| Layer        | Tech |
|-------------|------|
| Frontend    | Next.js 14 (App Router), TypeScript |
| Backend     | FastAPI, Pydantic v2, SQLAlchemy 2.0 (sync) |
| Queue       | Celery + Redis |
| DB          | PostgreSQL 16 |
| Migrations  | Alembic |

Scraping: unknown domains use a best-effort **GenericProductScraper** (HTTP first, structured data and HTML heuristics, then Playwright fallback). **Technopolis Bulgaria** (`technopolis.bg`) uses a site-specific adapter with OCC/API, HTTP, and Playwright layers and stores real `PriceSnapshot` rows (see below).

## Quick start (Docker Compose — development)

This Compose file is tuned for **local development**: source trees are **bind-mounted** so you can edit on the host without rebuilding images on every change. Ports and env vars match the previous setup (`8000` / `3000`, same `DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS`, `NEXT_PUBLIC_API_URL`).

**Important — working directory:** Run every `docker compose` command from the **repository root** (the folder that contains `docker-compose.yml`).

The Compose project name is fixed as **`pricing-app`** (`name:` in `docker-compose.yml`).

### First-time / after `requirements.txt` changes (backend)

Build the Python image (Playwright + deps), then start:

```bash
docker compose build backend
docker compose up -d
```

**Day to day:** After editing Python or Next.js code on disk, **do not rebuild** — **uvicorn `--reload`** and **Next.js `dev`** pick up changes. Rebuild **only** when `backend/requirements.txt` or `backend/Dockerfile` changes:

```bash
docker compose build backend celery_worker
docker compose up -d
```

### Frontend `node_modules`

`./frontend` is mounted at `/app`, and **`node_modules` lives in a named volume** (`frontend_node_modules`) so the Linux container does not use Windows/macOS binaries from the host. On container start, `npm install` runs once (fast afterward). If dependencies look wrong, reset the volume:

```bash
docker compose down
docker volume rm pricing-app_frontend_node_modules
docker compose up -d
```

### Celery

The worker mounts `./backend` like the API container. Celery does not auto-reload task code; after changing Python modules under `tasks/` or `scrapers/`, restart:

```bash
docker compose restart celery_worker
```

### Migrations

Apply database migrations **once** (after Postgres is healthy):

```bash
docker compose run --rm backend alembic upgrade head
```

### Full stack restart

```bash
docker compose down
docker compose up -d
```

If **backend**, **frontend**, or **celery_worker** exit immediately, check logs:  
`docker compose logs backend --tail 100` (and `frontend`, `celery_worker`). Common causes: failed image build, missing migrations, or port `3000` / `8000` already in use on the host.

Then open:

- Frontend: `http://localhost:3000` (home redirects to **`/products`**)
- API docs: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`

### Technopolis (real adapter)

1. Add a **Competitor** whose `domain` is `technopolis.bg` (with or without `www.`; both match).
2. Under **Price monitor**, add a listing whose URL is a **product detail** page on `https://www.technopolis.bg/...`.
3. Run **Run scrape** (or enqueue via API). The worker loads the page with **Playwright**, parses BGN prices, and writes a snapshot. Failures are recorded in `raw_data`, with optional screenshots under `backend/storage/scrape_failures/`.

**CLI test** (from repo root, after `pip install` + `playwright install chromium`):

```bash
python backend/scripts/test_technopolis_scraper.py "https://www.technopolis.bg/bg/..."
```

You should see JSON with `title`, `price`, `old_price`, `promo_price`, `currency`, `availability`, `image_url`, and `raw_data` (selectors used, timings, and parse hints).

Logs use the markers `scraper_start`, `scraper_success`, and `scraper_failure` (with `duration_ms`) for filtering.

### Try the placeholder scrape

1. Create a **Product** and a **Competitor** in the UI.
2. Under **Price monitor**, add a competitor **URL** (any unique string works).
3. Click **Run scrape** — the Celery worker writes a demo `PriceSnapshot`.
4. Refresh **Dashboard** — lowest competitor price and “difference %” update when the product is linked to that listing.

## Local development without Docker (optional)

### Backend

```bash
cd backend
python -m venv .venv
.\.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env          # adjust DATABASE_URL / REDIS_URL if needed
alembic upgrade head
python -m playwright install chromium
uvicorn app.main:app --reload --port 8000
```

On Linux, prefer `python -m playwright install chromium --with-deps` once (the Docker image runs this during build).

Start Redis locally and set `REDIS_URL` accordingly before launching Celery:

```bash
celery -A app.celery_app worker --loglevel=info
```

### Frontend

```bash
cd frontend
copy .env.example .env.local   # NEXT_PUBLIC_API_URL=http://localhost:8000
npm install
npm run dev
```

Open **http://localhost:3000** (not only 127.0.0.1) unless you also add that origin to backend CORS.

## Troubleshooting: "Failed to fetch"

The browser could not reach the FastAPI server (or the response was blocked before JavaScript could read it). Work through this checklist:

1. **Backend is running** — open [http://localhost:8000/docs](http://localhost:8000/docs). You should see Swagger UI.
2. **Health check in browser** — [http://localhost:8000/health](http://localhost:8000/health) returns `{"status":"ok"}`.
3. **Sidebar health widget** — every page shows **Backend reachable** / **Backend unreachable** with a **Test connection** button (`GET /health` with `mode: "cors"`, `credentials: "omit"`).
4. **If `/docs` works but frontend fetch fails** — this is usually **not** “backend down”. Check:
   - **CORS** — your UI origin must be in `CORS_ORIGINS` (e.g. `http://localhost:3000` vs `http://127.0.0.1:3000` are different origins).
   - **Request origin** — open DevTools → **Network**, click the failed request, compare **Request URL** and **Origin** header with backend logs (`incoming request method=… path=… origin=…` when `DEBUG=true`).
   - **Credentials mode** — frontend uses `credentials: "omit"`; backend uses `allow_credentials=False`. Do not mix `credentials: "include"` without matching CORS.
   - **Browser console / Network tab** — look for `(blocked:cors)`, `OPTIONS` preflight failures, or wrong port.
5. **Frontend API URL** — in `frontend/.env.local` (local dev) or Docker Compose `frontend.environment`:

   ```env
   NEXT_PUBLIC_API_URL=http://localhost:8000
   ```

   **Do not** use `http://backend:8000` for `NEXT_PUBLIC_API_URL` — the browser runs on your machine and cannot resolve Docker service names.
6. **Restart frontend after env changes** — `NEXT_PUBLIC_*` variables are read when the Next.js dev server starts:

   ```bash
   # local
   cd frontend && npm run dev

   # Docker
   docker compose restart frontend
   ```
7. **CORS defaults** — `backend/.env.example` and Compose include:

   ```env
   CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001,http://localhost:3001,http://127.0.0.1:3001
   ```

8. **Local backend `.env`** — when running `uvicorn` on the **host**, use `localhost` in `DATABASE_URL` / `REDIS_URL`, not `postgres` / `redis` hostnames (those are for containers only). Set `DEBUG=true` to log every request origin.
9. **Docker stack** — Postgres and Redis must be up; backend logs: `docker compose logs backend --tail 50`.
10. **Competitors page debug panel** — on load failure, the UI shows the API base URL, endpoint path, and error message.

## Project layout

```text
backend/
  app/
    main.py              # FastAPI app + router includes
    config.py            # Settings (env-driven)
    database.py          # Engine, SessionLocal, Declarative Base
    celery_app.py        # Celery instance
    models/              # SQLAlchemy models
    schemas/             # Pydantic request/response models
    routers/             # HTTP routers
    services/            # DB / domain helpers + `product_import.py`
    tasks/               # Celery tasks
    scrapers/            # Base + registry + demos + Technopolis adapter
  alembic/               # Migration scripts
  requirements.txt
  Dockerfile
  .env.example

frontend/
  app/                   # Next.js routes (dashboard, products, …)
  components/
  lib/
    config.ts            # API_BASE_URL (NEXT_PUBLIC_API_URL)
    api.ts               # Fetch wrapper / API client
  Dockerfile
  .env.example

docker-compose.yml       # dev: bind mounts + uvicorn --reload + next dev (see Quick start)
```

## Primary HTTP routes

| Area | Method | Path |
|------|--------|------|
| Products | CRUD | `/products`, `/products/{id}` (list: `?limit=75&offset=0`, max `limit=100`) |
| Products | POST | `/products/import-xlsx` (multipart `file` field) |
| Products | GET | `/products/template-xlsx` |
| Products | GET | `/products/price-comparison` (paginated) |
| Products | GET | `/products/{id}/prices` |
| Competitors | CRUD | `/competitors`, `/competitors/{id}` |
| Competitors | GET | `/competitors/tree` |
| Competitors | POST | `/competitors/{competitor_id}/discover-categories` (202, Celery) |
| Categories | GET | `/competitor-categories/{category_id}/products` — `sort_by=last_checked`, `scraped=true/false`, etc. |
| Categories | POST | `/competitor-categories/{category_id}/discover-products` (202) |
| Categories | POST | `/competitor-categories/{category_id}/scrape-prices` (202) |
| Categories | POST | `/competitor-categories/{category_id}/find-matches` (202) |
| Listings | CRUD-ish | `/competitor-products`, `/competitor-products/{id}` (list paginated) |
| Listings | GET | `/competitor-products/overview` (paginated) |
| Listings | POST | `/competitor-products/{id}/find-matches` |
| Matches | POST | `/matches/confirm`, `/matches/reject` |
| Snapshots | GET | `/price-snapshots?competitor_product_id=…` |
| Jobs | POST | `/jobs/scrape-product/{competitor_product_id}` |
| Dashboard | GET | `/dashboard/products` (legacy; prefer `/products/price-comparison` in the UI) |

## XLSX product import

Own-catalog rows can be bulk-loaded from Excel **`.xlsx`** (synchronous MVP — no queue).

1. **Download template** — `GET /products/template-xlsx` or open **Integrations** in the app. The first row must list exactly these columns (header names are matched case-insensitively; spaces become underscores):  
   `sku`, `ean`, `brand`, `name`, `category`, `manufacturer_code`, `own_price`

2. **Fill rows** — **`sku`** and **`name`** are required. **`own_price`** is optional; invalid numbers are rejected for that row. Empty cells become `null` in the database.

3. **Upload** — `POST /products/import-xlsx` with `multipart/form-data` and field name **`file`**, or use **Integrations** (`/integrations`) in the frontend.

4. **Behaviour** — Rows are validated per line. If **`sku` already exists**, that product is **updated** (same fields as import); otherwise a new product is created. The JSON response is:

   ```json
   { "total_rows", "imported_rows", "skipped_rows", "errors": [{ "row", "message" }] }
   ```

5. **Database** — Run Alembic after pulling changes so `products.manufacturer_code` exists:  
   `alembic upgrade head`

## Database performance

### Migrations

Apply all indexes (including `pg_trgm` on `products.name` and `competitor_products.title` when using PostgreSQL):

```bash
docker compose run --rm backend alembic upgrade head
```

The revision `20260520_0006` adds composite and lookup indexes idempotently (`CREATE INDEX IF NOT EXISTS` / inspect-before-create). It is safe to re-run on databases that already have some indexes from earlier revisions.

### EXPLAIN helper

From the repository root, with Postgres populated:

```bash
docker compose run --rm backend python scripts/explain_core_queries.py
```

The script prints `EXPLAIN (ANALYZE, BUFFERS)` for:

- Competitor category product page (workspace SQL)
- Competitor all-products workspace page
- Products price comparison (latest price batch)
- Latest price lookup per listing
- Match candidate lookup (EAN / manufacturer code / catalog batch)

Set `DATABASE_URL` if not using Docker defaults.

### List endpoint page sizes

Paginated list endpoints accept `limit` (default **75**, max **100**) and `offset` (default **0**):

| Endpoint | Response shape |
|----------|----------------|
| `GET /products` | `{ rows, total, limit, offset, has_more }` |
| `GET /products/price-comparison` | same |
| `GET /competitor-products` | same |
| `GET /competitor-products/overview` | same |
| `GET /dashboard/products` | same |
| `GET /competitor-categories/{id}/products` | workspace page (already paginated) |
| `GET /competitors/{id}/products` | workspace page |

No list endpoint returns unbounded rows; use export or background jobs for full dumps.

### Latest scrape fields and price history

Migration `20260521_0007` adds `competitor_products.latest_*` columns. The workspace table reads these fields directly (default sort: `latest_scraped_at DESC NULLS LAST`).

By default, `PRICE_HISTORY_ENABLED=false` in `backend/.env` / Docker Compose — scrapes update listing columns only and do **not** insert new `price_snapshots` rows. Set `PRICE_HISTORY_ENABLED=true` to keep historical snapshots (as before).

Apply migration:

```bash
docker compose run --rm backend alembic upgrade head
docker compose restart backend celery_worker
```

## Environment files

| File | Purpose |
|------|---------|
| `.env.example` | Root pointer — where to configure backend vs frontend |
| `backend/.env.example` | Local FastAPI/Celery on host (`localhost` DB/Redis) + CORS |
| `backend/.env` | Your local backend copy (gitignored) — use **`localhost`**, not `postgres` |
| `frontend/.env.example` | Template for browser API URL |
| `frontend/.env.local` | **Preferred** for local `npm run dev` — `NEXT_PUBLIC_API_URL=http://localhost:8000` |
| `frontend/.env` | Fallback if `.env.local` is missing |
| `docker-compose.yml` | Container env: internal `postgres`/`redis` hostnames; frontend still uses `http://localhost:8000` for the browser |

**Browser API URL (always):** `NEXT_PUBLIC_API_URL=http://localhost:8000`

**Docker backend (containers only):**

```env
DATABASE_URL=postgresql+psycopg2://postgres:postgres@postgres:5432/pricing_monitor
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001
```

## Notes

- The SPA uses **Integrations** (`/integrations`), **Competitors** (`/competitors`), and **Products** (`/products`) with a left sidebar. Legacy routes `/dashboard`, `/price-monitor`, `/products/import`, and `/scrape-jobs` redirect into these modules.
- **Auth** and **billing** are out of scope for this skeleton.
- `GET /dashboard/products` remains for backwards compatibility; the **Products** page calls `GET /products/price-comparison`.
