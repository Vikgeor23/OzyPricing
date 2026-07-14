# Ozypricing — Setup & Handover Guide

> **BG накратко:** Това е брандирано копие на pricing апликацията (оранжева тема, име
> „Ozypricing"). По-долу е как да се подкара от git на нов сървър и как да се
> прехвърли базата данни от оригиналния сървър. Логото (`logo.png`/`favicon.png`)
> все още е старото — трябва да се регенерира (виж «Known TODO»).

This is a rebrand of an existing price-monitoring app. Only the **frontend**
branding was changed:
- Accent color **purple → orange** (`frontend/app/globals.css`, `frontend/public/favicon.svg`).
- Product name **"Pricemancer" → "Ozypricing"** (layout title, sidebar, login, copy).
- `docker-compose.yml` project name changed `pricing-app → ozypricing` so this
  stack uses its **own** containers + Postgres volume and never touches the
  original app's data.

The backend, scrapers, DB schema and business logic are **unchanged** from the origin.

---

## 1. Stack overview

| Component | Tech | How it runs |
|---|---|---|
| Postgres 16 | docker | `docker compose` service `postgres` (volume `pgdata`) |
| Redis 7 | docker | `docker compose` service `redis` |
| Backend API | FastAPI (uvicorn) | `docker compose` service `backend` → :8000 |
| Worker | Celery | `docker compose` service `celery_worker` |
| Frontend | Next.js 14 | Node on the host: `npm run build && npm start` → :3000 |

The frontend is **not** containerized (the Docker `frontend` service is commented
out in `docker-compose.yml`). Run it with Node on the host.

---

## 2. Prerequisites

- Docker + Docker Compose v2
- Node.js 18+ and npm (for the frontend)
- `git`
- To transfer the DB: access to the **original** server's Postgres container.

---

## 3. Clone

```bash
git clone <REPO_URL> ozypricing
cd ozypricing
```

`node_modules/`, `.next/`, `.env*`, `__pycache__/` and DB dumps are gitignored —
you must install deps and create env files after cloning (steps below).

---

## 4. Environment files

**Backend** (`backend/.env`) — copy the template and keep the Docker-internal hostnames:
```bash
cp backend/.env.example backend/.env
```
For the Docker stack, the compose file already injects
`DATABASE_URL=postgresql+psycopg2://postgres:postgres@postgres:5432/pricing_monitor`
and `REDIS_URL=redis://redis:6379/0` — you normally don't need to edit `backend/.env`
when running via compose.

**Frontend** (`frontend/.env.local`) — point the browser at the backend API:
```bash
cp frontend/.env.example frontend/.env.local
# edit frontend/.env.local:
#   local dev:  NEXT_PUBLIC_API_URL=http://localhost:8000
#   production: NEXT_PUBLIC_API_URL=https://<your-domain>/api
```
> ⚠️ The origin app's `.env.local` pointed at `https://api.pricemancer.com`. That is
> **not** committed (gitignored). Always set your own `NEXT_PUBLIC_API_URL`.

Also update backend CORS to allow your frontend origin: in `docker-compose.yml`,
service `backend`, env `CORS_ORIGINS` (comma-separated origins).

---

## 5. Start the backend stack

```bash
docker compose build backend       # first time, or after Python-dep changes
docker compose up -d postgres redis
docker compose up -d backend celery_worker
```

Compose project name is `ozypricing`, so containers are
`ozypricing-postgres-1`, `ozypricing-backend-1`, `ozypricing-celery_worker-1`, etc.

### Database schema

Migrations are **not** auto-run by the container. Pick ONE:

- **A. Fresh empty DB** (no data): run Alembic once —
  ```bash
  docker compose exec backend alembic upgrade head
  ```
- **B. Restore data from the original DB** (recommended — carries all
  competitors/products/prices). See section 6. A restored dump already contains
  the schema, so you do **not** run `alembic upgrade head` on top of a fresh restore
  (only run it afterwards if the code has newer migrations than the dump).

---

## 6. Transfer the database from the original server

The origin DB is ~**3.2 GB** (`pricing_monitor`, user `postgres`). A compressed
custom-format dump is much smaller and restores fastest.

### 6a. Dump on the ORIGINAL server
Container there is `pricing-app-postgres-1`:
```bash
docker exec pricing-app-postgres-1 \
  pg_dump -U postgres -Fc -d pricing_monitor \
  > ozypricing_db.dump
```
`-Fc` = compressed custom format. This file is gitignored — do **not** commit it.

### 6b. Move the dump to the NEW server
```bash
scp ozypricing_db.dump user@new-server:/path/to/ozypricing/
```
(or rsync / object storage — anything out-of-band; not git.)

### 6c. Restore on the NEW server
With the `ozypricing` postgres container running:
```bash
# copy the dump into the container (or bind-mount it), then:
docker exec -i ozypricing-postgres-1 \
  pg_restore -U postgres -d pricing_monitor --clean --if-exists --no-owner \
  < ozypricing_db.dump
```
`--clean --if-exists` drops existing objects first so the restore is repeatable.
`--no-owner` avoids role-ownership mismatches.

### 6d. (optional) Apply newer migrations
If this repo's code has migrations newer than the dump:
```bash
docker compose exec backend alembic upgrade head
```

> **Redis** carries only transient Celery queue state — it does **not** need
> transferring. Start it empty.

---

## 7. Start the frontend

```bash
cd frontend
npm install
npm run build
npm start          # serves on 0.0.0.0:3000
# or for development:
npm run dev        # http://localhost:3000
```

Make sure `frontend/.env.local` `NEXT_PUBLIC_API_URL` points at the backend
reachable **from the browser** (not a Docker-internal hostname).

---

## 8. Running on the SAME host as the original app (avoid clashes)

The compose project name is already isolated (`ozypricing`), so containers and the
Postgres **volume are separate** — the original app's data is safe. **But the host
ports still collide.** Before `docker compose up` on a shared host, remap the host
side of these ports in `docker-compose.yml` (change only the left number):

| Service | Original | Suggested for Ozypricing |
|---|---|---|
| postgres | `5432:5432` | `5433:5432` |
| redis | `6379:6379` | `6380:6379` |
| backend | `8000:8000` | `8001:8000` |

If you remap the backend port, set `frontend/.env.local`
`NEXT_PUBLIC_API_URL=http://localhost:8001` (and dump/restore commands still use the
in-container port 5432). Also run the frontend on a free port
(`next start -p 3001`). On a dedicated server none of this is needed.

---

## 9. Brand assets

- The old raster logo (`logo.png`) and PNG favicon (`favicon.png`) were **removed**.
  The header and login now render an **orange text wordmark "Ozypricing"**
  (`.brand-wordmark` in `globals.css`). No broken images.
- `favicon.svg` is the only icon, recolored to orange — wired in `app/layout.tsx`.
- **Optional:** to use a graphical logo instead of the text wordmark, drop an orange
  `logo.png` into `frontend/public/` and swap the `<span class="brand-wordmark">`
  back to `<Image src="/logo.png" .../>` in `components/SidebarLayout.tsx` and
  `app/login/page.tsx` (re-add `import Image from "next/image"`).
- Backend CORS still lists `pricemancer.com` domains in `docker-compose.yml` — update
  to your real domain(s).

---

## 10. Quick smoke test

```bash
curl -s http://localhost:8000/api/health          # backend up
docker compose exec backend alembic current        # schema at head
# open the frontend, confirm orange theme + "Ozypricing" name
```
