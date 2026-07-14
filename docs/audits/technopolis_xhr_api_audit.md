# Technopolis Product Price API / XHR Audit

**Date:** 2026-05-21  
**Mode:** Read-only (no scraper changes)  
**Goal:** Find internal JSON/XHR endpoints that supply PDP price, stock, and identifiers so batch scraping can avoid full Playwright rendering per product.

---

## Executive Summary

Technopolis is a **SAP Commerce Cloud (Hybris) SPA** backed by a public **OCC REST API** at `https://api.technopolis.bg`. The storefront declares this in HTML:

```html
<meta name="occ-backend-base-url" content="https://api.technopolis.bg">
```

On product page load, the browser calls **`GET …/products/{productCode}`** (JSON) as the primary data source. That endpoint returns **price, stock, EAN, title, brand, images, classifications, and variant metadata**.

**Replay with httpx is highly feasible:** all five audited PDPs returned **HTTP 200** and numeric prices **without cookies or Playwright**, using only `User-Agent`, `Accept: application/json`, `Accept-Language: bg-BG`, and `Referer: https://www.technopolis.bg/`.

**Recommendation:** Add an **OCC JSON fast path** before Playwright: parse product id from `/p/{id}`, `GET` the OCC product resource, map `price` + `stock` + `ean` into `ScrapeResult`. Expected latency **~0.3–1.5 s/product** vs **10–35 s** Playwright, supporting **70–120+/min** at moderate concurrency.

---

## Methodology

1. **Product URL selection** — Five live PDPs from `Product-bg-EUR-0.xml` sitemap, one per category prefix (TV accessories, TV other, DVD players, musical instruments, projection screens).
2. **Playwright network capture** — `backend/scripts/audit_technopolis_xhr.py` listens to all `xhr`/`fetch` responses during PDP navigation (domcontentloaded + price-selector waits).
3. **Logged per request** — URL, method, status, content-type, first 1000 chars of body, heuristic flags (price / availability / EAN / product code / stock / variants).
4. **httpx replay** — Direct `GET` to top `api.technopolis.bg` candidates without browser session.
5. **Parameter minimization** — Additional probes on `fields`, `lang`, `curr`, `servicingStore`, `postalCode`.

Raw capture: `docs/audits/technopolis_xhr_raw.json`  
httpx validation: `docs/audits/occ_probe_all.json`, `docs/audits/occ_probe_14251.json`, `docs/audits/occ_param_probe.json`

---

## Tested Product URLs

| # | Category (sitemap prefix) | Product URL | OCC `productCode` |
|---|---------------------------|-------------|-------------------|
| 1 | TV-stojki | https://www.technopolis.bg/bg/TV-stojki/TV-Stojka--HAMA-108726/p/14251 | `14251` |
| 2 | TV-aksesoari-drugi | https://www.technopolis.bg/bg/TV-aksesoari-drugi/PCMCI-CARD-CONAX-CAM/p/16307 | `16307` |
| 3 | Blu-Ray-i-DVD-pleari | https://www.technopolis.bg/bg/Blu-Ray-i-DVD-pleari/DVD-PLEAR-SONY-DVP-SR760/p/33033 | `33033` |
| 4 | Muzikalni-instrumenti | https://www.technopolis.bg/bg/Muzikalni-instrumenti/KLASIChESKA-KITARA-YAMAHA-C30/p/156468 | `156468` |
| 5 | Ekrani | https://www.technopolis.bg/bg/Ekrani/Ekran-HAMA-TRIPOD-SCREEN-125--WH/p/64200 | `64200` |

**URL → API id rule:** trailing segment after `/p/` is the OCC product code (e.g. `/p/14251` → `14251`).

---

## Platform & API Base

| Item | Value |
|------|--------|
| API host | `https://api.technopolis.bg` |
| Base path | `/videoluxcommercewebservices/v2/technopolis-bg` |
| Style | SAP Commerce OCC (custom `videolux` extension) |
| Base site id | `technopolis-bg` |

---

## Candidate API Endpoints

### 1. Primary — Product detail (USE FOR SCRAPING)

| Field | Value |
|-------|--------|
| **URL pattern** | `GET https://api.technopolis.bg/videoluxcommercewebservices/v2/technopolis-bg/products/{productCode}` |
| **Method** | `GET` |
| **Status (audit)** | `200` on all 5 products (browser + httpx) |
| **Content-Type** | `application/json` (+ `charset=UTF-8` on replay) |
| **Price** | **Yes** — `price.value` (number), `price.currencyIso`, `price.formattedValue` |
| **Stock** | **Yes** — `stock.stockLevel`, `stock.stockLevelStatus` (`inStock`, `reserved`, etc.) |
| **EAN** | **Yes** — `ean` (nullable on some SKUs) |
| **Product code** | **Yes** — `code` (matches URL id) |
| **Specs** | **Yes** — `classifications[]` (FULL / expanded fields) |
| **Variants** | **When applicable** — `baseOptions`, `variantOptions`, `variantsValuesMap`, `multidimensional` |
| **httpx replay** | **Yes — no cookies required** |

**Browser request (observed on PDP load):**

```http
GET /videoluxcommercewebservices/v2/technopolis-bg/products/14251
  ?fields=DEFAULT,averageRating,images(FULL),manufacturer,numberOfReviews,
          categories(FULL),baseOptions,baseProduct,variantOptions,variantType,
          ecoImages(FULL),potentialPromotions(FULL),bundlePromotions(FULL),
          classifications,ean,brand
  &imageFormats=videoluxProduct,videoluxGrid,videoluxZoom,videoluxThumbnail
  &lang=bg&curr=EUR&servicingStore=1302&postalCode=1000
Accept: application/json, text/plain, */*
Accept-Language: bg-BG
Referer: https://www.technopolis.bg/
```

**Minimal httpx replay (validated):**

```http
GET /videoluxcommercewebservices/v2/technopolis-bg/products/14251?fields=FULL&lang=bg&curr=EUR
```

Works with **empty query** and **`fields=DEFAULT` only** (price still returned in probes).

**Sample response shape (truncated):**

```json
{
  "code": "14251",
  "name": "TV Стойка  HAMA 108726  ЧЕРЕН",
  "ean": "4047443136848",
  "brand": "HAMA",
  "url": "/TV-stojki/TV-Stojka--HAMA-108726/p/14251",
  "price": {
    "currencyIso": "EUR",
    "formattedValue": "30,10 €",
    "value": 30.1,
    "priceType": "BUY",
    "channel": "offline"
  },
  "stock": {
    "stockLevel": 0,
    "stockLevelStatus": "reserved"
  },
  "purchasable": true,
  "availableForPickup": true,
  "classifications": [ "..." ],
  "images": [ "..." ]
}
```

**httpx results (all 5 products):**

| productCode | HTTP | price (EUR) | stockLevelStatus | ean |
|-------------|------|-------------|------------------|-----|
| 14251 | 200 | 30.1 | reserved | 4047443136848 |
| 16307 | 200 | 30.5 | inStock | null |
| 33033 | 200 | 54.9 | inStock | 4905524842074 |
| 156468 | 200 | 149.9 | inStock | 4957812496858 |
| 64200 | 200 | 66.4 | inStock | 4007249187901 |

---

### 2. Secondary — Product references (NOT needed for core scrape)

| Field | Value |
|-------|--------|
| **URL pattern** | `GET …/products/{productCode}/references?referenceType={ACCESSORIES\|SIMILAR\|NEW\|CROSSELLING\|…}` |
| **Purpose** | Cross-sell / accessories carousels |
| **Price** | On **referenced** products (`target.price` when fields request it), not primary PDP |
| **Stock / EAN** | On referenced targets |
| **httpx replay** | Yes (200 without cookies) |
| **Scrape use** | Skip for price monitoring |

---

### 3. CMS — Product page shell (NOT for price)

| Field | Value |
|-------|--------|
| **URL pattern** | `GET …/cms/pages?pageType=ProductPage&code={productCode}&lang=bg&curr=EUR&servicingStore=1302&postalCode=1000` |
| **Purpose** | Angular layout slots, SEO text |
| **Price / stock / EAN** | **No** |
| **httpx replay** | Yes |

---

### 4. CMS — Top navigation (NOT for price)

| Field | Value |
|-------|--------|
| **URL pattern** | `GET …/cms/components/topnavigation/topNavigationBarMenu?lang=bg&curr=EUR&servicingStore=1302&postalCode=1000` |
| **Purpose** | Global menu |
| **Price** | **No** |

---

### 5. Noise (ignore for scraping)

Captured but irrelevant: LiveChat, TikTok pixel, Google GSI, creative CDN tags, analytics pings. These fire on PDP load but do not carry catalog price.

---

## Network Capture Statistics (Playwright)

| Product | xhr/fetch captured | api.technopolis.bg calls | Notes |
|---------|-------------------|--------------------------|-------|
| 14251 | 31 | Product + references + cms | 1 gzip decode warning in listener |
| 16307 | ~30 | Same pattern | |
| 33033 | ~30 | Same pattern | |
| 156468 | ~30 | Same pattern | |
| 64200 | ~30 | Same pattern | |

**Per-PDP pattern (consistent):**

1. `cms/pages?ProductPage&code={id}` — layout  
2. **`products/{id}?fields=…`** — **primary catalog payload**  
3. Multiple `products/{id}/references?referenceType=…` — merchandising  
4. `cms/components/topnavigation/…` — menu  

---

## Headers & Cookies

### Browser → OCC (observed)

| Header | Required for replay? | Example |
|--------|----------------------|---------|
| `User-Agent` | Recommended | Chrome 120 Windows |
| `Accept` | Recommended | `application/json, text/plain, */*` |
| `Accept-Language` | Optional | `bg-BG` |
| `Referer` | Optional (sent by browser) | `https://www.technopolis.bg/` |
| `Cookie` | **Not required** in httpx probes | — |
| `Authorization` | **None** | Public catalog read |

### Query parameters

| Param | Browser default | Required? | Notes |
|-------|-----------------|-----------|-------|
| `lang` | `bg` | Soft | Bulgarian copy |
| `curr` | `EUR` | Soft | Prices returned in EUR |
| `servicingStore` | `1302` | Soft | Store context; price unchanged in probes |
| `postalCode` | `1000` | Soft | Sofia default in storefront |
| `fields` | Long DEFAULT+ list | Soft | `FULL` or `DEFAULT` both return `price` |

**Conclusion:** No session cookie jar needed for read-only product GET. Use stable `lang=bg&curr=EUR` (and optionally `servicingStore` / `postalCode` to match storefront).

---

## Field Availability Matrix

| Data | OCC `products/{id}` | `/references` | CMS pages |
|------|---------------------|---------------|-----------|
| **Price** | ✅ `price.value` | ⚠️ on related SKUs only | ❌ |
| **Old / promo price** | ✅ `showOldPrice`, promotions objects | ⚠️ partial | ❌ |
| **Stock** | ✅ `stock.*` | ✅ on targets | ❌ |
| **Availability label** | Derive from `stockLevelStatus`, `purchasable`, `soldOut` | Partial | ❌ |
| **EAN** | ✅ `ean` | ✅ on targets | ❌ |
| **Product code** | ✅ `code` | ✅ | ❌ |
| **Title** | ✅ `name` | — | ❌ |
| **Brand** | ✅ `brand` | — | ❌ |
| **Specs** | ✅ `classifications` | — | ❌ |
| **Variants** | ✅ `baseOptions`, `variantOptions`, maps | — | ❌ |
| **Images** | ✅ `images[]` | — | ❌ |
| **Breadcrumbs / category** | ✅ `breadcrumbDatas`, `categories` | — | ❌ |

---

## Replay Feasibility (httpx)

| Endpoint | Replay | Latency (observed) | Cookies | Risk |
|----------|--------|-------------------|---------|------|
| `products/{id}` | ✅ **High** | Sub-second | None | Rate limits unknown; variant SKUs need code resolution |
| `products/{id}/references` | ✅ | Sub-second | None | Not needed for monitoring |
| `cms/pages` | ✅ | Sub-second | None | No price |

**Why current HTML httpx path fails but OCC works:**

- `www.technopolis.bg` PDP HTML often returns **404 with large SPA shell** (no hydrated price in static HTML).
- OCC host returns **200 JSON** with structured `price` regardless of SPA routing.

---

## Mapping to Scrape Pipeline

```text
competitor_product.url
  → regex /p/(\d+)/  → productCode
  → GET api.technopolis.bg/.../products/{productCode}?fields=FULL&lang=bg&curr=EUR
  → if 200 and price.value:
        ScrapeResult(price, currency, ean, availability from stockLevelStatus, title=name, ...)
        scrape_layer = "occ_api"
     else:
        existing Playwright path (fallback)
```

**Availability mapping (suggested):**

| OCC | Internal |
|-----|----------|
| `stockLevelStatus == "inStock"` | `in_stock` |
| `reserved`, `lowStock` | map per business rules |
| `soldOut` / `purchasable == false` | `out_of_stock` |

**Currency:** API returns **EUR** (`currencyIso`). Convert or store EUR consistently with existing `BGN` display rules.

---

## Risks & Edge Cases

1. **Configurable / multi-SKU products** — May require `baseProduct` + `variantOptions` or a variant-specific `code` instead of URL id. Audit samples were non-configurable (`baseOptions: []`). Spot-check phones/laptops before rollout.
2. **Rate limiting** — Not hit during audit (5–10 sequential calls). Batch at 70–120/min may need polite concurrency + backoff.
3. **Price channel** — Sample shows `"channel": "offline"`; confirm online vs offline price parity for monitoring.
4. **Missing EAN** — e.g. product `16307` returned `ean: null`; handle gracefully.
5. **Deleted products** — OCC may return 404 JSON; align with existing `product_not_found` handling.
6. **Gzip responses** — Some browser responses are gzip-compressed; httpx decompresses automatically. Playwright listener should use `response.body()` + decode for future audits.

---

## Recommended Implementation Plan

### Phase 1 — OCC fast path (highest ROI)

1. Add `technopolis_occ.py` (or module under `scrapers/sites/`) with:
   - `extract_product_code(url) -> str | None`
   - `fetch_product_occ(product_code) -> dict | None` via shared `httpx.AsyncClient`
2. Integrate in `technopolis_hybrid.scrape_technopolis_url()` **before** Playwright (do not remove Playwright yet):
   - On success → `scrape_layer=occ_api`, record metrics
   - On failure → existing Playwright pool path
3. Config: `SCRAPE_OCC_ENABLED=true`, `SCRAPE_OCC_BASE_URL`, default query params (`lang`, `curr`, `servicingStore`, `postalCode`).

### Phase 2 — Metrics & validation

1. Extend batch metrics: `occ_api_success`, `avg_occ_ms`, compare failure rate vs Playwright-only.
2. Run A/B on 500–1000 URLs: OCC vs Playwright price match tolerance (e.g. ±0.01 EUR).

### Phase 3 — Variants & specs (optional)

1. For `configurable: true`, resolve selected variant from URL or default option.
2. Map `classifications` → `specs_json` if parity with BS4 parser needed.

### Phase 4 — Reduce Playwright pool load

1. If OCC success rate >95% and price match verified, demote Playwright to fallback only (timeouts, OCC 404, variant edge cases).
2. Target throughput **70–120/min** with OCC at concurrency 20–40 (I/O bound, not browser bound).

---

## Appendix: Audit Tooling

| Artifact | Path |
|----------|------|
| Playwright capture script | `backend/scripts/audit_technopolis_xhr.py` |
| Raw JSON capture | `docs/audits/technopolis_xhr_raw.json` |
| httpx product probes | `docs/audits/occ_probe_all.json` |
| Parameter probe | `docs/audits/occ_param_probe.json` |

**Re-run capture:**

```powershell
cd C:\Pricing-App\backend
py scripts/audit_technopolis_xhr.py --json-out ..\docs\audits\technopolis_xhr_raw.json
```

---

## Conclusion

Technopolis product prices are **not secrets hidden behind Playwright** — they are served from a **documented OCC JSON API** (`api.technopolis.bg`) that the SPA already calls. A direct **httpx OCC fast path** is the correct way to avoid rendering every PDP in Chromium, with Playwright retained as fallback for variant edge cases and validation failures.
