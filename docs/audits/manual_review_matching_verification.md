# Manual review matching UX — read-only verification audit

**Date:** 2026-05-22  
**Scope:** Static inspection of listed frontend/backend files. No code changes. No runtime UI or API calls.  
**Environment note:** `npm` and `pytest` were not available in the audit environment; full Next.js `tsc`/build and automated tests were not executed.

---

## Executive summary

The manual review flow is **largely wired correctly end-to-end**: workspace rows expose `top_candidates` and `matched_product_id`, the review modal (`openReviewChooser`) loads candidates and falls back to `POST /competitor-products/{id}/find-matches` for **needs_review** and **low_confidence** when candidates are empty, confirm/reject call the expected match endpoints with valid bodies, and the Products module shows linked competitor listings with match metadata from `price_comparison_service.py`.

**Gaps vs. the 14-point checklist:**

| Area | Verdict |
|------|---------|
| Modal for `needs_review` / `low_confidence` | Pass (with find-matches fallback) |
| Modal for `no_candidate` | Opens, but **no** auto find-matches; different title/CTA |
| Rejected workspace filter + post-reject refresh | **Fail** — backend never surfaces `match_status=rejected` on workspace rows |
| Build / `tsc` | Not verified (tooling missing) |
| Runtime E2E | Not verified |

**Overall:** Safe to use for confirm/reject and Products price comparison; treat **Rejected** filter and transient **Rejected** badge after refresh as known inconsistencies until backend workspace status includes rejected rows or UI stops advertising that filter.

---

## What works

### Competitors workspace (`frontend/app/competitors/page.tsx`)

- **`isReviewableMatchStatus`** — Returns true only for `needs_review` and `low_confidence` (lines 141–144).
- **`openReviewChooser`** — Sets `chooseForId` / `chooseRow`, seeds `chooseCandidates` from `row.top_candidates`, and when empty + reviewable calls `api.post<FindMatchesResponse>(`/competitor-products/${id}/find-matches`, {})` (lines 817–845). Modal renders via `chooseForId` block (lines 2136–2297).
- **`top_candidates` display** — Table maps `MatchCandidate` fields (name, sku, ean, brand, score, method, reasons/warnings) with **Confirm match** → `confirmMatch` (lines 2163–2224).
- **`confirmMatch` / `manualPickProduct`** — `POST /matches/confirm` with `product_id`, `competitor_product_id`, `match_score`, `match_method` (no `status` in body; server sets `confirmed` in `match_service.upsert_match_and_link_product`) (lines 1216–1289).
- **`rejectMatch` / `rejectListingNoMatch`** — `POST /matches/reject` with `product_id`, `competitor_product_id`, `reason: null` (lines 1292–1360). `rejectListingNoMatch` resolves `product_id` from `matched_product_id`, first `top_candidates` entry, or find-matches.
- **`patchWorkspaceRow`** — Shallow-merge patch per `competitor_product_id` without dropping unrelated fields (lines 800–808).
- **`refreshWorkspace`** → **`fetchWorkspacePage`** — Replaces `workspace` from paginated GET (`setWorkspace(page.rows)`) (lines 433–475, 504–508).
- **Status UX** — `matchStatusLabel`, `statusBadgeClass`, and workspace match filter chips include `auto_matched`, `needs_review`, `low_confidence`, `no_candidate`, `confirmed`, `rejected` (lines 108–127, 1886–1925).
- **Row actions** — **Review match** vs **Choose product** based on `isReviewableMatchStatus` (lines 2019–2023).
- **Catalog load** — `ensureCatalogLoaded` uses `GET /products?limit=100` and `page.rows` (`ProductListPage`) (lines 811–814).

### Types (`frontend/lib/types.ts`)

- **`CategoryWorkspaceProduct`** — `matched_product_id`, `top_candidates`, optional match metadata aligned with `CategoryWorkspaceProduct` schema.
- **`CompetitorPriceLine`** — Optional listing URL/title/prices/scrape time and `match_status` / `match_score` / `match_method`.
- **`MatchCandidate`** — Matches `app.schemas.match.MatchCandidate` field set (scores as `string` on client; API may return JSON numbers — runtime coercion is typical).

### Backend workspace (`backend/app/schemas/category_workspace.py`, `backend/app/services/workspace_query.py`)

- Schema includes `matched_product_id`, `top_candidates: list[MatchCandidate]`.
- **`_build_workspace_select`** / **`_row_to_schema`** — Populate match fields from `best_match_subquery()`; `parse_top_candidates` in `workspace_match_fields.py`.
- **`_effective_status_expr`** — Filter param `status` compared to effective status (confirmed if `CompetitorProduct.product_id` set, else best match status, else `no_candidate`).

### Match API (`backend/app/routers/matches.py`, `backend/app/services/match_service.py`, `backend/app/schemas/match.py`)

- **`POST /matches/confirm`** — `MatchConfirmBody`; upsert sets `status="confirmed"` and `cp.product_id = body.product_id`.
- **`POST /matches/reject`** — `MatchRejectBody`; sets `status="rejected"`, clears `cp.product_id` when it matched rejected product.

### Find matches (`backend/app/routers/competitor_products.py`)

- **`find_matches_for_listing`** — `POST /{competitor_product_id}/find-matches`, `response_model=FindMatchesResponse`, top 5 ranked candidates.

### Products module (`frontend/app/products/page.tsx`, `backend/app/services/price_comparison_service.py`)

- **`CompetitorListingsTable`** — Nested table per `competitor_prices` with title, URL, price, promo, scraped time, match status, score, method (lines 59–107).
- **`_batch_linked_cp_ids`** — Union of direct `CompetitorProduct.product_id` links and `ProductMatch` rows with `status != "rejected"`; **set** dedupes per product (lines 54–94).
- **`_batch_best_match_by_cp`** — Same ranking as workspace via `best_match_subquery()` (lines 97–108, `app/db/latest_price.py` lines 49–79).
- **`build_price_comparison_page`** — One `CompetitorPriceLine` per linked `cp_id`; match metadata from best match or `confirmed` when only `cp.product_id` is set (lines 250–294).

### Build / syntax (limited)

- **`python3 -m compileall -q app`** on backend succeeded.
- IDE linter on touched frontend files timed out; no diagnostic result.

---

## Possible regressions

1. **Rejected workspace status (P1)** — `best_match_subquery()` excludes `rejected` (`ProductMatch.status != "rejected"`). After reject, workspace effective status becomes **`no_candidate`**, not `rejected`. UI filter `status=rejected` uses `_effective_status_expr(best) == "rejected"`, which is **never true** → **Rejected** filter shows empty. Optimistic `patchWorkspaceRow` sets `match_status: "rejected"` but **`refreshWorkspace` overwrites** with server `no_candidate`.

2. **`no_candidate` review modal (P1)** — Modal opens (`openReviewChooser` always sets `chooseForId`), title **Choose catalog product**, no auto find-matches (only `isReviewableMatchStatus`). Users rely on **Find match** (legacy `findForId` panel) or catalog search. Checklist item “fallback find-matches when top_candidates empty” applies only to reviewable statuses.

3. **Dual UX paths (P2)** — Legacy inline **Find match** panel (`findForId`, `runFindMatches`, lines ~1185–2134) coexists with review modal (`chooseForId`). Same APIs, two UIs.

4. **Optimistic vs server copy (P2)** — `match_reason` strings like `"Confirmed manually"` / `"Rejected — no match"` differ from DB `ProductMatch.match_reason` until refresh; brief flash possible if refresh is slow.

5. **`rejectListingNoMatch` on bare `no_candidate` (P2)** — May reject the **first find-matches candidate** as the rejected pair, not a user-visible “no match” without a product id.

6. **Catalog cap (P2)** — `ensureCatalogLoaded` loads only first 100 products; manual pick may miss SKUs beyond that page.

7. **Stale `chooseRow` (P2)** — Modal uses `chooseRow` captured at open; if `refreshWorkspace` runs elsewhere, modal snapshot can drift until closed.

8. **Decimal JSON vs TS `string` (P2)** — FastAPI may serialize `Decimal` as numbers; frontend types use `string` for scores/prices. Usually renders; strict `tsc` might flag if run with strict excess property checks.

---

## API contract verification

| Endpoint | Caller | Body / response | Backend handler | Match |
|----------|--------|-----------------|-----------------|-------|
| `POST /matches/confirm` | `confirmMatch`, `manualPickProduct` | `{ product_id, competitor_product_id, match_score, match_method }` | `matches.confirm_match` → `match_service.upsert_match_and_link_product` | Yes |
| `POST /matches/reject` | `rejectMatch`, `rejectListingNoMatch` | `{ product_id, competitor_product_id, reason: null }` | `matches.reject_match` → `match_service.reject_match` | Yes |
| `POST /competitor-products/{id}/find-matches` | `openReviewChooser`, `rejectListingNoMatch`, `runFindMatches` | `{}` → `FindMatchesResponse` | `competitor_products.find_matches_for_listing` | Yes |
| `GET .../products?` (workspace) | `fetchWorkspacePage` | `CategoryWorkspacePage` | `workspace_query.paginate_workspace` / category or competitor routers | Yes |
| `GET /products/price-comparison` | `ProductsComparisonPage.loadPage` | `PriceComparisonPage` | `price_comparison_service.build_price_comparison_page` | Yes |

**Confirm:** Client does not send `status`; server forces `confirmed`. **Reject:** Optional `reason` nullable per `MatchRejectBody`.

---

## Frontend state verification

| Concern | Implementation | Assessment |
|---------|----------------|------------|
| Modal open | `setChooseForId` in `openReviewChooser` | OK for all statuses |
| Candidate source | `row.top_candidates` then conditional find-matches | OK for needs_review / low_confidence only |
| Optimistic update | `patchWorkspaceRow` then `void refreshWorkspace()` | Merge is safe; full replace on refresh can revert `rejected` → `no_candidate` |
| Row identity | Keyed by `competitor_product_id` | OK |
| Busy state | `busy === cpId` disables actions | OK |
| Error surfaces | `chooseError`, `actionError` | OK |

**Functions/components (competitors page):** `workspaceProductsQuery`, `isReviewableMatchStatus`, `openReviewChooser`, `closeReviewChooser`, `patchWorkspaceRow`, `confirmMatch`, `rejectMatch`, `rejectListingNoMatch`, `manualPickProduct`, `ensureCatalogLoaded`, `fetchWorkspacePage`, `refreshWorkspace`, `matchStatusLabel`, `statusBadgeClass`, review modal (`chooseForId`).

---

## Backend data verification

| Concern | Location | Behavior |
|---------|----------|----------|
| `matched_product_id` | `workspace_query._build_workspace_select` (`best.c.product_id`) | Exposed on workspace rows |
| `top_candidates` | `best.c.top_candidates` → `parse_top_candidates` | JSON list → `MatchCandidate` list |
| Best match ranking | `app/db/latest_price.py` `best_match_subquery` | Excludes rejected; ranks confirmed > auto_matched > needs_review > low_confidence |
| Effective status filter | `workspace_query._effective_status_expr` | Never `rejected` |
| Rejected in price table | `price_comparison_service._batch_linked_cp_ids` | Rejected matches omitted from link set |
| Direct CP link after reject | `match_service.reject_match` | Clears `cp.product_id` when equal to rejected product |

**Schemas:** `category_workspace.CategoryWorkspaceProduct`, `price_comparison.CompetitorPriceLine`, `match.MatchConfirmBody` / `MatchRejectBody` / `MatchCandidate` — consistent with service outputs.

---

## Products module verification

- **Display:** `CompetitorListingsTable` in `frontend/app/products/page.tsx` shows all `competitor_prices` lines for each `PriceComparisonRow`.
- **Linkage:** Non-rejected `ProductMatch` plus `CompetitorProduct.product_id` direct links; rejected rows excluded from match query loop.
- **Duplication:** Per-product `set` of `cp_id` prevents duplicate lines for the same listing when both direct link and match row exist.
- **Rejected listings:** Should not appear as active matched products when reject cleared `product_id` and match is `rejected`; if `cp.product_id` still set without a match row, direct link could still show (reject path clears when ids align).

---

## P0 / P1 / P2 issues

| Priority | Issue | Evidence |
|----------|-------|----------|
| **P1** | Workspace **Rejected** filter and post-refresh status do not show `rejected` | `_effective_status_expr` + `best_match_subquery` exclude rejected; `fetchWorkspacePage` replaces optimistic `rejected` with `no_candidate` |
| **P1** | Checklist fallback find-matches for **`no_candidate`** not implemented in `openReviewChooser` | Only `isReviewableMatchStatus` triggers POST find-matches (lines 828–833) |
| **P2** | Duplicate find/confirm UX (`findForId` vs `chooseForId`) | Two panels in same page |
| **P2** | Catalog manual pick limited to 100 products | `ensureCatalogLoaded` |
| **P2** | `rejectListingNoMatch` may bind reject to first scored candidate | find-matches when no `matched_product_id` |
| **P2** | Type looseness: API `Decimal` vs frontend `string` | `types.ts` vs Pydantic JSON |
| **—** | **P0** | None identified from static analysis alone |

---

## Manual test checklist

Use a category with batch-matched listings in mixed statuses.

### Workspace modal

- [ ] Row `needs_review` with `top_candidates` → **Review match** → modal title **Manual match review** → candidates table matches workspace data.
- [ ] Row `needs_review` with empty `top_candidates` → modal loads → network shows `POST .../find-matches` → candidates appear.
- [ ] Row `low_confidence` — same as above.
- [ ] Row `no_candidate` → **Choose product** → modal title **Choose catalog product** → **no** auto find-matches on open → catalog search / separate **Find match** still work.
- [ ] Confirm from modal → `POST /matches/confirm` payload has correct UUIDs and `match_method` → row becomes **Confirmed** after refresh; Products page shows listing under product.
- [ ] Reject from modal → `POST /matches/reject` → listing drops from Products competitor table for that product; workspace row shows **No candidate** (not **Rejected**) after refresh.
- [ ] **Reject / No match** on `no_candidate` without prior candidate → find-matches or error message as designed.

### Filters and badges

- [ ] Filters: auto_matched, needs_review, low_confidence, no_candidate, confirmed return expected rows.
- [ ] Filter **Rejected** — expect **empty** until backend exposes rejected on workspace (documented gap).
- [ ] Badge classes: confirmed (success), auto_matched (info), needs_review / low_confidence (warn), no_candidate (danger).

### Products module

- [ ] Expand competitor listings — title, URL, prices, scraped time, match status labels match workspace/DB.
- [ ] Same listing not duplicated twice on one product row.
- [ ] Rejected match no longer listed under product.

### Regression / build (when tooling available)

- [ ] `cd frontend && npm run build` — zero TypeScript errors.
- [ ] `cd backend && pytest` — match/workspace/price comparison tests pass.
- [ ] Smoke: batch match → needs_review → confirm → verify Products + workspace consistent.

---

## Files inspected

| Path | Role |
|------|------|
| `frontend/app/competitors/page.tsx` | Review modal, optimistic patch, match actions |
| `frontend/app/products/page.tsx` | `CompetitorListingsTable`, price comparison page |
| `frontend/lib/types.ts` | Shared TS types |
| `backend/app/schemas/category_workspace.py` | Workspace row schema |
| `backend/app/schemas/price_comparison.py` | Price line schema |
| `backend/app/services/workspace_query.py` | Workspace query + row mapping |
| `backend/app/services/price_comparison_service.py` | Products price comparison builder |
| `backend/app/routers/matches.py` | Confirm/reject routes |
| `backend/app/services/match_service.py` | Confirm/reject persistence |
| `backend/app/db/latest_price.py` | `best_match_subquery` (referenced) |
| `backend/app/routers/competitor_products.py` | find-matches endpoint (referenced) |
| `backend/app/schemas/match.py` | Request/response bodies (referenced) |

---

## Audit method limitations

- No browser or API runtime verification.
- No `npm run build` / `tsc` (npm not installed in audit shell).
- No pytest execution.
- Recommend completing the manual test checklist in a dev environment with seed data before production reliance on **Rejected** filtering.
