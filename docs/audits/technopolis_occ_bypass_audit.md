# Technopolis OCC API Bypass Audit

**Date:** 2026-05-21  
**Mode:** Read-only (no scraper logic changes)  
**Symptom:** Batch UI shows `OCC API: 0`, `HTTP skipped: 100`, `Playwright fallback: 0`, `Avg / product ~16s`.

---

## Executive Summary

Two separate issues explain the metrics:

1. **OCC success is not visible in the UI (confirmed bug)** — Batch code *does* compute `occ_api_success`, `occ_api_failed`, and `avg_occ_ms`, but **`GET /competitors/scrape-tasks/{task_id}` never returns them**. The Pydantic `ScrapeTaskStatus` schema and router only expose legacy fields (`lightweight_success`, `playwright_fallback`, `http_skipped`, …). The frontend therefore **always displays OCC as 0**, even when OCC succeeds in Celery.

2. **~16s average strongly suggests Playwright still runs in the scrape worker** — Local verification shows OCC returns price in **&lt;1s** for real DB URLs. If the UI avg is ~16s, the worker is likely still spending time on Playwright (or timing out there), not on successful OCC. That points to **runtime/environment** (Celery worker code age, network to `api.technopolis.bg`, or OCC branch not enabled in the worker process) — not a broken parser on happy path.

**`HTTP skipped: 100` is expected** when `SCRAPE_HTTP_ENABLED=false`: every scrape attempt sets `http_skipped=True` in `raw_data`, and metrics increment `http_skipped` per attempt — not per failed HTTP.

**`Playwright fallback: 0` does not prove Playwright unused** — On success, metrics count `js_extract_success` *before* `playwright_fallback`. Playwright + JS fast path increments **`js_extract_success` only** (also not exposed in the API schema).

---

## Flow Traced

```text
Celery scrape_competitor_products_batch
  → run_batch_scrape_competitor_products (scraping_batch.py)
    → fetch_scrape_result_for_listing (scrape_fetch.py)
      → scrape_technopolis_url (technopolis_hybrid.py)
          1. if settings.scrape_occ_enabled:
               scrape_technopolis_occ (technopolis_occ_api.py)
               → success: _success_result(layer="occ_api")
               → fail: diagnostics["occ_api_failed"]=True, continue
          2. if scrape_http_enabled: HTTP parse (off by default)
          3. Playwright pool / single-page fallback
    → metrics.record(result)  # counts occ_api_success when layer=="occ_api"
    → progress_callback(_live_metrics())  # includes occ_* in meta dict
  → Celery update_state(meta=...)

Frontend poll:
  GET /competitors/scrape-tasks/{task_id}
    → get_scrape_task_status builds ScrapeTaskStatus WITHOUT occ_* fields
    → UI shows occ_api_success ?? 0  → always 0
```

---

## 1. Config / env flags

| Setting | Default in code | `backend/.env` | `docker-compose.yml` (backend + celery) |
|---------|-----------------|----------------|----------------------------------------|
| `scrape_occ_enabled` | `True` | **not set** (uses default `True`) | `SCRAPE_OCC_ENABLED: "true"` |
| `scrape_http_enabled` | `False` | not set | `SCRAPE_HTTP_ENABLED: "false"` |

**Local probe (host Python):**

```text
scrape_occ_enabled True
scrape_http_enabled False
OCC fetch for /p/14251 → status 200, price 30.1, no fallback
```

**Implication:** Code and defaults are correct on the host. If Celery runs in Docker without restarted workers after OCC deploy, or without `SCRAPE_OCC_ENABLED`, behavior can differ.

---

## 2. OCC branch execution (`technopolis_hybrid.py`)

Relevant block (lines ~331–354):

- Runs when `settings.scrape_occ_enabled` is true.
- Calls `scrape_technopolis_occ(url)`.
- On non-`None` result → `_success_result(..., layer="occ_api")` and **returns immediately** (Playwright not called).
- On failure → `diagnostics["occ_api_failed"] = True`, falls through to Playwright.

**Not discarded later:** OCC success returns before HTTP/Playwright. `_success_result` sets `scrape_layer` to `"occ_api"` after merging `result.raw_data` (which also sets `scrape_layer` / `source`).

---

## 3. Product code extraction (`technopolis_occ_api.py`)

Regex: `/p/(\d+)(?:[/?#]|$)`

**DB sample (15 recent Technopolis PDPs):** all extract valid numeric codes (`302640`, `491118`, `14898`, …). No systematic `no_product_code` from URL shape.

**Edge case:** homepage row `https://www.technopolis.bg/bg/` → `None` (not a PDP).

---

## 4. OCC request URL

Built as:

```text
GET https://api.technopolis.bg/videoluxcommercewebservices/v2/technopolis-bg/products/{productCode}
  ?fields=FULL&lang=bg&curr=EUR
```

Headers: `User-Agent`, `Accept: application/json`, `Accept-Language: bg-BG`, `Referer: https://www.technopolis.bg/`

**Local:** 200 + JSON with `price.value` for tested IDs.

**Docker/Celery:** Not verified in this audit run. If the worker cannot reach `api.technopolis.bg`, `fetch_occ_product` returns `status=0` and `occ_fallback_reason` like connection error → Playwright path → ~16s.

---

## 5. OCC parse (`parse_occ_product_payload`)

Returns `None` only when **missing price or name**:

```python
if price is None or not name:
    return None
```

Otherwise builds full `ScrapeResult` with `raw_data.source = "occ_api"`.

**Not the cause on host** for standard PDPs (verified with fixture + live API).

---

## 6. Batch metrics counters (`scraping_batch.py`)

| Counter | When incremented |
|---------|------------------|
| `occ_api_success` | Success + `scrape_layer_from_result() == "occ_api"` |
| `occ_api_failed` | `raw_data.get("occ_api_failed")` on any recorded attempt |
| `http_skipped` | `raw_data.get("http_skipped")` — **every attempt** when HTTP disabled |
| `playwright_fallback` | Success + layer `"playwright"` **and** not `parse_mode == "js_evaluate"` |
| `js_extract_success` | Success + `parse_mode == "js_evaluate"` |

`scrape_layer_from_result` includes `"occ_api"` (scrape_fetch.py).

**Metrics are computed correctly in the batch task meta** passed to `progress_callback`.

---

## 7. API / UI gap (root cause for “OCC API: 0” in UI)

### `ScrapeTaskStatus` schema (`app/schemas/scrape_batch.py`)

Defines: `lightweight_success`, `playwright_fallback`, `http_skipped`, …  
**Does not define:** `occ_api_success`, `occ_api_failed`, `avg_occ_ms`, `js_extract_success`, adaptive fields.

### Router (`app/routers/competitors.py` `get_scrape_task_status`)

Manually maps only legacy fields into `ScrapeTaskStatus`. **Omits all OCC fields**, even though `meta` from Celery `update_state` contains them.

### Frontend (`frontend/lib/types.ts`)

TypeScript **includes** `occ_api_success`, `occ_api_failed`, `avg_occ_ms`, but API never sends them → UI shows **0**.

### Celery final `result` payload

`run_batch_scrape` return dict **includes** `occ_api_success` via `_live_metrics()`. That lives in `status.result` in TS, but the progress panel reads **`scrapeAllProgress.occ_api_success`**, not `result.occ_api_success`.

**Conclusion:** **You cannot use current UI metrics to prove OCC is unused.** They only prove OCC metrics are **not wired to the poll endpoint**.

---

## 8. Why ~16s avg if OCC were working?

If OCC succeeded for most products:

- Per-product time would be ~0.3–2s (HTTP JSON), not ~16s.
- `avg_occ_ms` would appear in meta (but not in UI).
- Playwright pool might not start for most URLs (early return).

**16s avg indicates:**

- Playwright navigation + waits still dominate, **or**
- High timeout/failure rate with ~16s per failed attempt.

**Playwright fallback: 0** with long avg suggests:

- Many **failures** (not counted in `playwright_fallback`), and/or
- Successes via **`js_extract_success`** (Playwright used, not counted as `playwright_fallback`), and/or
- Low success rate overall.

---

## 9. Structured log points (recommended — not added in this audit)

To confirm runtime behavior in Celery logs without guessing:

| Event | When | Fields |
|-------|------|--------|
| `occ_start` | Enter OCC branch | `url`, `scrape_occ_enabled` |
| `occ_request_url` | Before GET | full OCC URL |
| `occ_response_status` | After GET | status, `product_code` |
| `occ_parse_success` | After parse | bool |
| `occ_missing_price` | parse None | bool |
| `occ_missing_name` | parse None | bool |
| `occ_success` | Returning OCC result | `price`, `duration_ms` |
| `occ_fallback_reason` | OCC miss | reason string |

Also log `scraper_success site=technopolis_bg layer=occ_api` (already present) vs `layer=playwright`.

---

## 10. Verification checklist (ops)

Run during next batch scrape:

1. **Celery worker logs** — search for `layer=occ_api` vs `layer=playwright`.
2. **Restart celery_worker** after deploying `technopolis_occ_api.py` / hybrid changes.
3. **Inside celery container:** `curl -I https://api.technopolis.bg/videoluxcommercewebservices/v2/technopolis-bg/products/14251?fields=FULL&lang=bg&curr=EUR`
4. **Inspect raw Celery meta** (Redis): confirm `occ_api_success` &gt; 0 while task running.
5. **Compare** `meta.occ_api_success` vs UI (UI will stay 0 until API schema fixed).

**Read-only diagnostic script:** `backend/scripts/audit_occ_runtime.py`  
**URL DB check:** `backend/scripts/check_urls.py`

---

## Findings table

| Check | Status | Notes |
|-------|--------|-------|
| `SCRAPE_OCC_ENABLED` in code | OK | Default true; compose sets true |
| OCC branch in hybrid | OK | First path, early return on success |
| productCode regex | OK | DB PDPs extract correctly |
| OCC URL | OK | Matches audit / live API |
| OCC parse on host | OK | Price + name present |
| Metrics `occ_api_success` in batch meta | OK | Implemented in `ScrapeBatchMetrics` |
| Metrics visible in UI | **FAIL** | API schema/router omit OCC fields |
| `http_skipped: 100` | Misleading | Per-attempt flag when HTTP off |
| `playwright_fallback: 0` | Inconclusive | JS path / failures / API omission |
| ~16s avg | **Likely Playwright/failures in worker** | Inconsistent with mass OCC success |

---

## Recommended next steps (after audit approval — not done here)

1. **Expose OCC metrics in API** — extend `ScrapeTaskStatus` + `get_scrape_task_status` (+ `js_extract_success`, adaptive fields).
2. **Add structured OCC logs** in `technopolis_occ_api.py` / hybrid (listed above).
3. **Confirm Celery runtime** — restart worker, test API reachability from container.
4. **Optional:** Rename UI label `HTTP skipped` → `HTTP disabled (per attempt)` to avoid confusion.

---

## Conclusion

The OCC implementation in **hybrid + OCC module + batch metrics** is wired logically and works on the host against real URLs. The UI showing **`OCC API: 0` is explained by an API response gap**, not by absence of batch counters.

The **~16s average** indicates the **worker is still not completing most scrapes via OCC** (or is failing/timeouting on Playwright). That requires **Celery log / network verification** — not visible from the current progress API.

Until the API exposes `occ_api_success` and logs confirm `layer=occ_api`, treat “OCC API: 0” in the UI as **non-diagnostic**, not proof the OCC path is unused.
