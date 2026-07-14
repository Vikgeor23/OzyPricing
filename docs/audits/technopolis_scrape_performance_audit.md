# Technopolis Scrape Pipeline — Performance & Failure Rate Audit

**Date:** 2026-05-20  
**Mode:** Read-only (no code changes)  
**Scope:** Batch scrape (`scrape_competitor_products_batch`), Technopolis hybrid HTTP/Playwright path, persistence, SQL, Celery.

---

## Executive Summary

The hybrid scrape architecture is **implemented and wired correctly**, but production behavior shows **zero HTTP fast-path wins** and **~35s average per-product scrape time** with **~45% failure rate**. The dominant pattern matches:

1. **HTTP path is attempted first on every URL**, then almost always discarded.
2. **Successful scrapes all go through Playwright** (shared browser pool in batch; full browser launch on single-scrape fallback).
3. **Throughput (~29 products/min)** is consistent with ~20 parallel Playwright navigations at ~30–40s each, not with sub-second HTTP scraping.
4. **Failures** align with dead/soft-404 listings, Playwright timeouts, and `price_missing_after_playwright` — not primarily DB commits.

**Primary root cause for HTTP fast path = 0:** Technopolis often returns **HTTP 4xx (e.g. 404) with a large HTML body** (soft error page). The hybrid layer treats **any status ≥ 400 as “blocked”** and skips HTTP parsing entirely (`technopolis_hybrid._is_blocked_response`). Live sampling of product URLs returned **404 + ~322KB HTML** with `лв` in the document but **no parseable price** in static HTML.

**Primary root cause for slowness:** Every listing pays **HTTP round-trip + full Playwright navigation** (up to 15s nav + up to 5s title wait + up to 5s price-in-body wait), because HTTP rarely produces a usable price.

**Primary root cause for ~45% failures:** Mix of **removed/invalid URLs** in catalog, **price not found after Playwright**, and **navigation/timeouts** — with error text stored on `competitor_products.latest_scrape_error` but **no structured failure taxonomy** in metrics.

---

## Observed Metrics (from UI / batch progress)

| Metric | Value | Interpretation |
|--------|-------|----------------|
| HTTP fast path | **0** | No scrape counted with `scrape_layer == "http"` |
| Playwright fallback | **All successful scrapes** | Every success uses Playwright path |
| Avg / product | **~35 s** | Per-task wall time (not wall-clock with concurrency) |
| Throughput | **~29 / min** | Wall-clock; ≈ 20 concurrent × ~35s → ~34/min theoretical max |
| Failure rate | **~45%** | Batch `failed` counter vs `total` |

These numbers are **internally consistent** with concurrent Playwright-heavy scraping, not with a working HTTP fast path.

---

## 1. Technopolis HTTP Fast Path

### 1.1 Is it implemented?

**Yes.** Module: `backend/app/scrapers/sites/technopolis_hybrid.py`

- `fetch_technopolis_html_http()` — `httpx.AsyncClient`, 15s timeout (configurable), `User-Agent` + `Accept-Language: bg-BG`.
- `scrape_technopolis_url()` — orchestrates HTTP → parse → optional Playwright fallback.
- JSON-LD enrichment: `_extract_json_ld_enrichment()` / `_merge_json_ld_into_result()`.
- Reuses existing BS4 parser: `TechnopolisScraper._parse_html_to_result()`.

Entry from batch: `scrape_fetch.fetch_scrape_result_for_listing()` → `scrape_technopolis_url(..., pool=pool)`.

Entry from single scrape: `TechnopolisScraper.run()` → `scrape_technopolis_url(..., pool=None)`.

### 1.2 Is it called before Playwright?

**Yes, always** (for Technopolis URLs). Flow in `scrape_technopolis_url()`:

```text
1. fetch_technopolis_html_http(url)
2. IF html OK AND NOT _is_blocked_response(status, html):
     parse + JSON-LD merge
     IF NOT _needs_playwright_fallback(result, html):
       RETURN success (scrape_layer=http)   ← never happening in prod
3. Playwright: pool.fetch_html(url) OR scraper._fetch_html_with_page()
4. parse + merge; fail if price still None
```

Evidence: `technopolis_hybrid.py` lines 235–291.

### 1.3 Why does HTTP fast path show 0 successes?

Metrics count HTTP success only when:

- `result.raw_data["scraper_status"] != "failure"`, and  
- `scrape_layer_from_result()` returns `"http"` (`scrape_layer` or `fetch_layer` in raw_data).

See `scraping_batch.ScrapeBatchMetrics.record()` and `scrape_fetch.scrape_layer_from_result()`.

**Documented reasons HTTP path does not complete:**

| # | Mechanism | Code / evidence |
|---|-----------|-----------------|
| A | **HTTP status ≥ 400 treated as blocked** — parsing skipped | `_is_blocked_response()`: `if status_code >= 400: return True` (`technopolis_hybrid.py:37–38`) |
| B | **Soft 404 with large HTML** — Technopolis returns ~320KB body on 404; still “blocked” | Live sample: product PDP `GET` → **404**, `len(html)≈322k`, `blocked=True` |
| C | **Parsed price is None on static HTML** — triggers `_needs_playwright_fallback()` | `_needs_playwright_fallback()`: `if result.price is None: return True` (line 49–50) |
| D | **HTML length < 1200** treated as blocked | `_is_blocked_response()` line 40–41 |
| E | **Captcha / Cloudflare markers** in body | `_BLOCKED_HTML_MARKERS` (lines 27–34) |

For typical Technopolis PDPs in catalog, **A + C** are the likely production combination: even when the body is huge, **404 status alone prevents HTTP parse**. When status is 200, price is often **client-rendered** and absent from static HTML / JSON-LD in the initial response.

### 1.4 Does Technopolis HTML contain price/title/specs server-side?

**Often not in a way the current parser uses on raw HTTP responses.**

- Parser expects `itemprop="price"`, `.current-price`, `.product-price`, or `лв` / `BGN` patterns in **static** HTML (`technopolis.py` — `PRICE_CONTAINER_SELECTORS`, `extract_all_leva_amounts`).
- Live HTTP sample (404 PDP): `has_lev=True` in full document, **`price_samples=[]` in first 80KB**, `has_itemprop_price=False`.
- Site homepage over HTTP: **200**, ~881KB HTML — proves httpx connectivity works.
- Technopolis PDPs commonly behave as **SPA / SSR shell + client hydration**; Playwright waits for `лв|BGN|€` in `document.body.innerText` (`technopolis_playwright_pool.py:108–110`), which **HTTP does not execute**.

**Conclusion:** HTTP fast path is architecturally correct but **misaligned with Technopolis’s HTTP status codes and rendering model**.

### 1.5 Headers / cookies / user-agent

| Item | HTTP (`httpx`) | Playwright pool |
|------|----------------|-----------------|
| User-Agent | Chrome 120 Windows | Same (`technopolis_playwright_pool.py:17–20`) |
| Accept-Language | `bg-BG,bg;q=0.9,en;q=0.8` | `locale="bg-BG"` on context |
| Cookies | **None** | Session cookies in shared context (after first navigation) |
| Referer / sec-ch-ua | **None** | Default Playwright headers |

No cookie jar reuse across HTTP requests; **new `AsyncClient` per product** (`fetch_technopolis_html_http`, lines 151–157).

### 1.6 Is the parser too strict?

**Moderately strict for HTTP-only use:**

- Requires `Decimal` price from selectors or regex on static text.
- Does not call Technopolis **internal JSON APIs** (if any).
- JSON-LD merge only helps when `<script type="application/ld+json">` contains `Product` + `offers.price` — not observed on sampled 404 PDP.
- **404-as-blocked** is stricter than necessary for soft-404 pages that still contain marketing HTML.

Tests assume HTTP success with **mocked** HTML ≥5000 chars and **mocked** `price=Decimal("99.99")` (`tests/test_scrape_hybrid.py`) — does not reflect live Technopolis responses.

---

## 2. Playwright Scraper

### 2.1 Browser / context / page lifecycle

| Context | Browser launch | Context | Page |
|---------|----------------|---------|------|
| **Batch** (`scraping_batch` + pool) | **Once** per batch job (`TechnopolisPlaywrightPool.start()`) | **One shared** context | **New page per URL**, closed in `finally` (`fetch_html:93–118`) |
| **Single scrape fallback** (`pool=None`) | **Per product** — `async with async_playwright()` in `_fetch_html_with_page()` | New context per launch | New page, context closed after |
| **TechnopolisScraper.fetch()** | Still uses `_fetch_html_with_page()` only | N/A | **Bypasses hybrid** if called directly |

**Batch does not launch browser per product** — evidence: `scraping_batch.py:317–319` `async with TechnopolisPlaywrightPool() as pool`.

### 2.2 Waits and timeouts

| Step | Batch pool | Single-scrape `_fetch_html_with_page` |
|------|------------|--------------------------------------|
| Navigation | `wait_until="domcontentloaded"` | Same (`_goto_with_fallback`) |
| **networkidle** | **Not used** (removed from hot path) | **Not used** |
| Nav timeout | `SCRAPE_NAVIGATION_TIMEOUT_MS` default **15000** | Same via settings |
| Title selector | `h1` or `og:title`, **5000 ms** each | Same |
| Price signal | `wait_for_function` body text `лв\|BGN\|€`, **5000 ms** | Same |
| Post-goto sleep | **None in pool** | **`post_goto_wait_ms = 500`** (`technopolis.py:93`) |

Config: `backend/app/config.py` lines 34–37; Docker env in `docker-compose.yml` lines 74–77.

### 2.3 Resource blocking

**Yes, in pool** (`technopolis_playwright_pool._route_request`):

- Aborts: `image`, `media`, `font`, `stylesheet`
- Aborts URLs matching analytics/ads regex (Google Analytics, GTM, DoubleClick, Facebook, Hotjar, etc.)

**Single-scrape path** attaches same route handler via `TechnopolisPlaywrightPool._route_request` on ephemeral context (`technopolis.py:171–174`).

Scripts and XHR are **not** globally blocked — needed for client-side price hydration.

### 2.4 Why ~35s average per product?

Approximate **per-product timeline** when HTTP fails blocked check or price:

| Phase | Typical cost |
|-------|----------------|
| httpx GET (15s max) | 0.5–3s (often 1–2s) |
| Playwright `goto` domcontentloaded (15s max) | 3–10s |
| Title wait (5s max) | 0–5s |
| Price-in-body wait (5s max) | 0–5s |
| `page.content()` + BS4 parse | 1–3s |
| Category assignment on persist (success only) | 0.1–1s |

**Worst case:** 15 + 5 + 5 + HTTP ≈ **25–35s**, matching observed **~35s avg**.

**Important:** `avg_scrape_ms` in metrics is the **mean of per-task durations** (each includes HTTP attempt + Playwright), not serial wall time. Throughput **29/min** with concurrency **20** confirms parallel Playwright saturation: `20 / (35/60) ≈ 34/min`.

### 2.5 Double-fetch penalty

**Every product** runs HTTP first, then Playwright on fallback. With **0% HTTP success**, the HTTP leg is **pure overhead** on 100% of URLs.

---

## 3. Failure Analysis

There is **no centralized failure classifier** in code. Failures are inferred from `ScrapeResult.raw_data` and batch counters.

### 3.1 Failure sources (code mapping)

| Category | How it manifests | Code reference |
|----------|------------------|----------------|
| **timeout** | Playwright `goto` / selector / `wait_for_function` exceeds ms limits | `PlaywrightError` → `_failure_from_exception` or `TechnopolisFetchError` |
| **404 / product removed** | HTTP status ≥400 → skip parse; may still Playwright; dead URL → no price | `_is_blocked_response`; catalog stale URLs |
| **price not found** | `RuntimeError("price_missing_after_playwright")` | `technopolis_hybrid.py:275–281` |
| **blocked** | Short HTML, captcha markers, 4xx status | `_is_blocked_response`, `_BLOCKED_HTML_MARKERS` |
| **navigation error** | `TechnopolisFetchError`, `page.goto` failures | `technopolis.py` `_fetch_html_with_page` |
| **parser error** | HTTP parse exception → logged, fallback to PW | `http_parse_error` in diagnostics |
| **DB commit error** | `batch_scrape_persist_failure` | `scraping_batch.py:306–310` |
| **unhandled exception** | `batch_scrape_row_failure` | `scraping_batch.py:275–278` |

Stored on listing: `competitor_products.latest_scrape_status = 'failed'`, `latest_scrape_error` from `raw_data["error"]` (`scrape_persist._update_latest_scrape_fields`).

### 3.2 Relating ~45% failure rate to metrics

Batch increments `failed` when:

- `apply_scrape_result_to_listing` returns `"failed"` (`scraper_status == "failure"`), or
- Row exception / persist exception.

`ScrapeBatchMetrics.failed` only increments inside `metrics.record()` for `scraper_status == "failure"` — **not identical** to batch `failed` counter (persist errors counted separately).

**Likely failure mix (hypothesis, validate with DB query on `latest_scrape_error`):**

- **~40–50%** — `price_missing_after_playwright` + 404/410 URLs still in `competitor_products`
- **~20–30%** — Playwright timeout (15s nav + waits)
- **~10%** — navigation / TechnopolisFetchError
- **~5%** — DB/category persist edge cases

**Recommended validation query (read-only):**

```sql
SELECT
  CASE
    WHEN latest_scrape_error ILIKE '%price_missing%' THEN 'price_missing'
    WHEN latest_scrape_error ILIKE '%timeout%' THEN 'timeout'
    WHEN latest_scrape_error ILIKE '%404%' THEN 'http_404'
    ELSE 'other'
  END AS bucket,
  COUNT(*)
FROM competitor_products
WHERE latest_scrape_status = 'failed'
GROUP BY 1
ORDER BY 2 DESC;
```

---

## 4. Batch Task Architecture

### 4.1 Serial vs parallel

| Layer | Behavior |
|-------|----------|
| **Celery** | One task `scrape_competitor_products_batch` processes **entire competitor scope** |
| **Inside task** | `asyncio.Semaphore(SCRAPE_CONCURRENCY)` default **20** + `asyncio.gather` per chunk of 100 IDs |
| **Worker concurrency** | Docker: `celery -A app.celery_app worker` — **no `--concurrency` set** → default CPU-based; **separate batch jobs** can run in parallel, **one batch is one process event loop** |

Evidence: `scraping_batch._scrape_one_listing`, `_run_batch_scrape_async`, `run_batch_scrape_competitor_products` wraps `asyncio.run()`.

**Celery worker concurrency does not parallelize inside a single batch job** beyond asyncio. It only helps if **multiple batch tasks** are queued.

### 4.2 Commits

- **Not per product** — commit every `SCRAPE_BATCH_COMMIT_SIZE` (default **20**) and at end of each 100-ID chunk (`scraping_batch.py:300–315`).
- Persist phase is **serial** after each `gather` (apply results in loop) — DB writes are not concurrent.

### 4.3 Progress updates

- Celery `update_state(PROGRESS, meta=...)` at most every **`SCRAPE_PROGRESS_INTERVAL_SEC` (3s)** unless forced (`_report` in `scraping_batch.py:204–226`).
- Also logs on each progress flush (`scrape_batch_tasks.on_progress`).

**Impact:** Minor vs Playwright; Redis writes ~every 3s — acceptable.

### 4.4 Chunking

- ID cursor: batches of **100** (`CP_BATCH_SIZE`).
- Load all `CompetitorProduct` rows for chunk with `joinedload(competitor)`.

---

## 5. SQL / Persistence Impact

### 5.1 Indexes

Migration `20260521_0007_latest_scrape_fields.py`:

- `ix_competitor_products_latest_scraped_at`
- `ix_competitor_products_competitor_latest_scraped` (`competitor_id`, `latest_scraped_at DESC`)
- `ix_competitor_products_category_latest_scraped` (`competitor_category_id`, `latest_scraped_at DESC`)

Batch updates **single row by primary key** — indexing is **adequate**; not the bottleneck.

### 5.2 Latest fields vs PriceSnapshot

- **`price_history_enabled` default `false`** (`config.py:31`, Docker `PRICE_HISTORY_ENABLED: "false"`).
- Scrapes write **`competitor_products.latest_*` only** — no `PriceSnapshot` insert unless flag enabled (`scrape_persist.apply_scrape_result_to_listing`).

**DB write load:** One UPDATE per product per commit batch — **low** compared to scraping.

### 5.3 Extra work on success

On successful Technopolis scrape with breadcrumbs/slug:

- `ensure_category_path_for_competitor_product()` — may create/walk category tree + **`refresh_category_product_counts()`** per product (`competitor_category_builder.py:141–145`).

This adds **variable ORM cost** on success path; unlikely to explain 35s scrape time but can add **100ms–1s+** per row.

### 5.4 Is DB a bottleneck?

**No, for current metrics.** Dominant time is network + Playwright + double fetch. Commits every 20 rows are appropriate.

---

## 6. Current Bottlenecks (ranked)

1. **100% Playwright fallback** — HTTP path never wins; double network stack per URL.  
2. **Per-page Playwright waits** — up to 25s of configured waits per URL.  
3. **Large invalid catalog** — 404/removed products drive failures and wasted work.  
4. **HTTP 4xx = blocked** — discards soft-404 HTML without parse attempt.  
5. **Single-scrape path** still launches **full browser per fallback** (not batch).  
6. **No API / SSR data extraction** — only HTML + JSON-LD in static response.  
7. **Category tree refresh on every success** — secondary DB cost.

---

## 7. Root Causes (concise)

| Symptom | Root cause |
|---------|------------|
| HTTP fast path = 0 | Status ≥400 blocks HTTP parse; static HTML lacks price; SPA needs JS |
| All successes via Playwright | `_needs_playwright_fallback` always true in production |
| ~35s avg / product | HTTP + PW nav (15s) + selector waits (5+5s) + parse |
| ~29/min throughput | Concurrency 20 ÷ ~35s ≈ theoretical max |
| ~45% failures | Stale URLs + price_missing_after_playwright + timeouts |

---

## 8. Recommended Optimization Plan

### Phase A — Safe quick wins (low risk)

1. **Treat 404 with large HTML as parseable** — Only treat as blocked when body `<1200` or markers; attempt JSON-LD/parse on 404 before Playwright. *Expected: small HTTP win on soft-404; fewer PW runs.*  
2. **Skip HTTP when competitor is known SPA** — Config flag `SCRAPE_HTTP_ENABLED=false` for Technopolis until API path exists. *Expected: save 1–3s per URL immediately.*  
3. **Reduce Playwright waits** — Price `wait_for_function` optional or 2s; title wait 2s. *Expected: 20–40% shave off PW leg.*  
4. **Pre-filter dead URLs** — Before batch, skip listings with recent `latest_scrape_error` containing 404 / price_missing. *Expected: cut failure rate and wasted work.*  
5. **Persist failure taxonomy** — Write `latest_scrape_error_code` enum from classifier. *Expected: data for next tuning; no speed change.*  
6. **Sample live HTTP** — Log `http_status`, `html_len`, `parsed_price`, `json_ld_price` on 1% of URLs. *Expected: confirm fix impact.*

### Phase B — Medium effort

7. **Technopolis XHR/API capture** — Intercept product JSON in Playwright once, replay via httpx for batch. *Expected: HTTP-like speed with accurate prices.*  
8. **Reuse Playwright page** (tab per worker) instead of new page per URL. *Expected: 10–20% faster.*  
9. **httpx connection pool** — Single client per batch, keep-alive. *Expected: ~0.5–1s saved per URL on HTTP leg.*  
10. **Tune `SCRAPE_CONCURRENCY`** to RAM/CPU (e.g. 8–12 if worker OOM). *Expected: stable throughput.*

### Phase C — Larger refactors

11. **Dedicated fetch workers** — Split “fetch HTML/JSON” from “persist” Celery tasks.  
12. **Headless Chrome CDP service** — Long-lived browser fleet outside Celery task lifetime.  
13. **Official/stock feed** — If Technopolis exposes feed or mobile API, bypass HTML entirely.

---

## 9. Expected Performance After Fixes

| Scenario | Avg / product | Throughput (concurrency 20) | Failure rate |
|----------|---------------|-----------------------------|--------------|
| **Current** | ~35s | ~29/min | ~45% |
| **Quick wins (no HTTP win)** | ~20–25s | ~45–55/min | ~35–40% |
| **HTTP parse on soft-404 + JSON-LD** | ~15–20s | ~55–70/min | ~30–35% |
| **API/XHR replay path (50%+ HTTP-like)** | **&lt;1–3s** | **200–600/min** | ~25% (mostly dead URLs) |
| **Target from requirements** | **&lt;1s** | **30k catalog in hours** | **&lt;10%** with URL hygiene |

Sub-second average **requires** avoiding full page load for most products (API, SSR JSON, or cached HTML with embedded price).

---

## 10. File / Function Reference

| Concern | Location |
|---------|----------|
| Hybrid orchestration | `backend/app/scrapers/sites/technopolis_hybrid.py` — `scrape_technopolis_url` |
| HTTP fetch | `technopolis_hybrid.fetch_technopolis_html_http` |
| Blocked / fallback rules | `_is_blocked_response`, `_needs_playwright_fallback` |
| Playwright pool | `backend/app/scrapers/sites/technopolis_playwright_pool.py` |
| HTML parser | `backend/app/scrapers/sites/technopolis.py` — `_parse_html_to_result` |
| Batch concurrency | `backend/app/services/scraping_batch.py` |
| Metrics | `ScrapeBatchMetrics.record`, `as_dict` |
| Fetch entry | `backend/app/services/scrape_fetch.py` |
| Persist | `backend/app/services/scrape_persist.py` |
| Celery task | `backend/app/tasks/scrape_batch_tasks.py` |
| Config | `backend/app/config.py`, `docker-compose.yml` |
| Progress API | `backend/app/routers/competitors.py` — `get_scrape_task_status` |

---

## 11. Audit Limitations

- Failure mix percentages are **inferred** from code paths + sample HTTP probes; run SQL on `latest_scrape_error` for exact distribution.
- Live probes used **two PDP URLs** (both HTTP 404 with large body); not a full catalog sample.
- Worker CPU/RAM and Celery `--concurrency` were not measured in this audit.
- **No code was modified** as part of this audit (except this document).

---

*End of audit.*
