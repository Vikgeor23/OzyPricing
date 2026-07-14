"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import {
  api,
  downloadApiFile,
  getSectionErrorMessage,
  isApiAbortError,
  isApiError,
  isApiJsonBody,
  readQueuedTaskResponse,
} from "@/lib/api";
import { API_BASE_URL } from "@/lib/config";
import { registerBackgroundTask } from "@/lib/backgroundTasks";
import { useApiHealth } from "@/contexts/ApiHealthContext";
import type {
  CategoryTreeNode,
  CategoryWorkspacePage,
  CategoryWorkspaceProduct,
  Competitor,
  CompetitorTreeItem,
  DiscoveryTaskStatus,
  FullDiscoveryResult,
  MatchCandidate,
  MatchTaskStatus,
  ScrapeTaskStatus,
  Product,
  CompetitorProductAddResponse,
} from "@/lib/types";

const WORKSPACE_PAGE_SIZES = [50, 75, 100] as const;
const DEFAULT_WORKSPACE_PAGE_SIZE = 75;

type WorkspaceScrapeFilter = "all" | "scraped" | "not_scraped";
type WorkspaceMatchFilter =
  | "all"
  | "auto_matched"
  | "needs_review"
  | "low_confidence"
  | "no_candidate"
  | "confirmed"
  | "rejected";
type WorkspacePopoverMenu = "scrape" | "match" | "pin" | "hide";

const WORKSPACE_SCRAPE_FILTER_OPTIONS: { value: WorkspaceScrapeFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "scraped", label: "Scraped" },
  { value: "not_scraped", label: "Not scraped" },
];

const WORKSPACE_MATCH_FILTER_OPTIONS: { value: WorkspaceMatchFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "auto_matched", label: "Auto matched" },
  { value: "needs_review", label: "Needs review" },
  { value: "low_confidence", label: "Low confidence" },
  { value: "no_candidate", label: "No candidate" },
  { value: "confirmed", label: "Confirmed" },
  { value: "rejected", label: "Rejected" },
];

type WorkspaceColumnKey =
  | "image"
  | "title"
  | "category_path"
  | "url"
  | "brand"
  | "code"
  | "ean"
  | "mfr_model"
  | "size"
  | "color"
  | "product_price"
  | "final_eur"
  | "regular_eur"
  | "promo_eur"
  | "old_list_eur"
  | "currency"
  | "availability"
  | "offered_by"
  | "delivered_by"
  | "last_checked"
  | "matched_sku"
  | "matched_name"
  | "score"
  | "match"
  | "status"
  | "reason"
  | "actions";

const WORKSPACE_COLUMNS: { key: WorkspaceColumnKey; label: string; pinWidth: number }[] = [
  { key: "image", label: "Image", pinWidth: 64 },
  { key: "title", label: "Title", pinWidth: 220 },
  { key: "category_path", label: "Category path", pinWidth: 220 },
  { key: "url", label: "URL", pinWidth: 210 },
  { key: "brand", label: "Brand", pinWidth: 100 },
  { key: "code", label: "Code", pinWidth: 110 },
  { key: "ean", label: "EAN", pinWidth: 130 },
  { key: "mfr_model", label: "Mfr / model", pinWidth: 150 },
  { key: "size", label: "Size", pinWidth: 90 },
  { key: "color", label: "Color", pinWidth: 100 },
  { key: "product_price", label: "Product price", pinWidth: 120 },
  { key: "final_eur", label: "Final EUR", pinWidth: 100 },
  { key: "regular_eur", label: "Regular EUR", pinWidth: 110 },
  { key: "promo_eur", label: "Promo EUR", pinWidth: 100 },
  { key: "old_list_eur", label: "Old/list EUR", pinWidth: 115 },
  { key: "currency", label: "Currency", pinWidth: 90 },
  { key: "availability", label: "Availability", pinWidth: 120 },
  { key: "offered_by", label: "Offered by", pinWidth: 140 },
  { key: "delivered_by", label: "Delivered by", pinWidth: 140 },
  { key: "last_checked", label: "Last checked", pinWidth: 140 },
  { key: "matched_sku", label: "Matched SKU", pinWidth: 120 },
  { key: "matched_name", label: "Matched name", pinWidth: 170 },
  { key: "score", label: "Score", pinWidth: 80 },
  { key: "match", label: "Match", pinWidth: 120 },
  { key: "status", label: "Status", pinWidth: 110 },
  { key: "reason", label: "Reason", pinWidth: 210 },
  { key: "actions", label: "Actions", pinWidth: 260 },
];

type DiscoveryMethod =
  | "sitemap"
  | "category_pagination"
  | "external_search"
  | "dynamic_endpoints"
  | "site_search"
  | "merchant_feeds"
  | "autocomplete";

const DISCOVERY_METHOD_OPTIONS: { id: DiscoveryMethod; label: string }[] = [
  { id: "sitemap", label: "Sitemap" },
  { id: "category_pagination", label: "Category pagination" },
  { id: "external_search", label: "External search" },
  { id: "dynamic_endpoints", label: "Product APIs" },
  { id: "site_search", label: "Site search" },
  { id: "merchant_feeds", label: "Merchant feeds" },
  { id: "autocomplete", label: "Autocomplete" },
];

const DISCOVERY_METHOD_LABEL: Record<string, string> = Object.fromEntries(
  DISCOVERY_METHOD_OPTIONS.map((m) => [m.id, m.label]),
);

function workspaceProductsQuery(
  limit: number,
  offset: number,
  opts?: { scraped?: WorkspaceScrapeFilter; matchStatus?: WorkspaceMatchFilter; search?: string },
): string {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    sort_by: "last_scraped_at",
    sort_dir: "desc",
  });
  if (opts?.scraped === "scraped") {
    params.set("scraped", "true");
  } else if (opts?.scraped === "not_scraped") {
    params.set("scraped", "false");
  }
  const search = opts?.search?.trim();
  if (search) {
    params.set("search", search);
  }
  const match = opts?.matchStatus;
  if (match && match !== "all") {
    if (match === "confirmed" || match === "rejected") {
      params.set("status", match);
    } else {
      params.set("status", match);
    }
  }
  return params.toString();
}

function workspaceScrapeFilterLabel(filter: WorkspaceScrapeFilter): string {
  if (filter === "scraped") return "scraped";
  if (filter === "not_scraped") return "not scraped";
  return "";
}

function workspaceMatchFilterLabel(filter: WorkspaceMatchFilter): string {
  if (filter === "auto_matched") return "auto matched";
  if (filter === "needs_review") return "needs review";
  if (filter === "low_confidence") return "low confidence";
  if (filter === "no_candidate") return "no candidate";
  if (filter === "confirmed") return "confirmed";
  if (filter === "rejected") return "rejected";
  return "";
}

type FindMatchesResponse = {
  competitor_product_id: string;
  candidates: MatchCandidate[];
};

function fmtMoney(v: string | null) {
  if (v == null) return "—";
  const n = Number(v);
  return Number.isNaN(n) ? String(v) : n.toFixed(2);
}

function fmtLastChecked(iso: string | null) {
  if (iso == null || iso === "") return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function ProductImagePreview({
  src,
  alt,
  label,
}: {
  src?: string | null;
  alt: string;
  label?: string;
}) {
  return (
    <div className="match-image-cell">
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt={alt} className="match-image" loading="lazy" decoding="async" />
      ) : (
        <div className="match-image match-image-placeholder" aria-label="No image">
          —
        </div>
      )}
      {label ? <span>{label}</span> : null}
    </div>
  );
}

const COUNTRY_LABELS: Record<string, string> = {
  BG: "Bulgaria",
  DE: "Germany",
  RO: "Romania",
  EU: "European Union",
  US: "United States",
};

function inferRetailerCountry(c: Pick<CompetitorTreeItem, "country" | "domain">): string {
  const explicit = c.country?.trim().toUpperCase();
  if (explicit) return explicit;
  const tld = c.domain.split(".").pop()?.trim().toUpperCase();
  return tld || "Other";
}

function countryGroupLabel(code: string): string {
  const upper = code.toUpperCase();
  return COUNTRY_LABELS[upper] ? `${COUNTRY_LABELS[upper]} (${upper})` : upper;
}

function normalizedRetailerDomain(domain: string): string {
  return domain.trim().replace(/^https?:\/\//i, "").replace(/^www\./i, "").split("/")[0];
}

function retailerLogoUrls(domain: string): string[] {
  const normalized = domain.trim().replace(/^https?:\/\//i, "").replace(/^www\./i, "").split("/")[0];
  if (!normalized) return [];
  return [
    `https://${normalized}/favicon.ico`,
    `https://www.google.com/s2/favicons?domain=${encodeURIComponent(normalized)}&sz=64`,
    `https://icons.duckduckgo.com/ip3/${encodeURIComponent(normalized)}.ico`,
  ];
}

function RetailerAvatar({ name, domain }: { name: string; domain: string }) {
  const [logoIndex, setLogoIndex] = useState(0);
  const normalizedDomain = normalizedRetailerDomain(domain);
  const logoUrls = retailerLogoUrls(normalizedDomain);
  const logoUrl = logoUrls[logoIndex] ?? null;
  const initials = name.slice(0, 2).toUpperCase();

  useEffect(() => {
    setLogoIndex(0);
  }, [normalizedDomain]);

  return (
    <span className={logoUrl ? "retailer-avatar retailer-avatar-logo" : "retailer-avatar"} aria-hidden>
      {logoUrl ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={logoUrl}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setLogoIndex((current) => current + 1)}
        />
      ) : (
        initials
      )}
    </span>
  );
}

function discoveryPhaseLabel(phase: string | null | undefined): string {
  switch (phase) {
    case "checking_site_reachability":
      return "Checking site is reachable…";
    case "probing_site":
      return "Probing site for best method…";
    case "reading_sitemap_index":
      return "Reading sitemap index…";
    case "reading_merchant_feeds":
      return "Reading merchant feeds…";
    case "probing_autocomplete":
      return "Enumerating autocomplete…";
    case "magento_graphql_products":
      return "Reading catalog API (Magento)…";
    case "parsing_product_sitemaps":
      return "Parsing product sitemap…";
    case "crawling_public_pages":
      return "Crawling public pages…";
    case "category_pagination":
      return "Checking category pagination…";
    case "searching_external_indexes":
      return "Searching external indexes…";
    case "sniffing_dynamic_endpoints":
      return "Sniffing product APIs…";
    case "site_search":
      return "Searching inside site…";
    case "waiting_external_rate_limit":
      return "Waiting for external index…";
    case "deduplicating":
      return "Deduplicating URLs…";
    case "checking_existing_urls":
      return "Checking existing URLs…";
    case "saving_new_products":
      return "Saving new products…";
    case "updating_categories":
      return "Updating categories…";
    case "completed":
      return "Discovery complete";
    case "cancelled":
      return "Discovery stopped — found URLs saved";
    default:
      return phase ? `Discovering (${phase})…` : "Discovering product URLs…";
  }
}

function statusBadgeClass(status: string | null) {
  const s = (status || "").toLowerCase();
  if (s === "confirmed" || s === "cheapest") return "badge badge-success";
  if (s === "auto_matched") return "badge badge-info";
  if (s === "needs_review") return "badge badge-warn";
  if (s === "low_confidence") return "badge badge-warn badge-low-confidence";
  if (s === "rejected") return "badge badge-muted";
  if (s === "no_candidate" || s === "no_match") return "badge badge-danger";
  return "badge badge-muted";
}

function matchStatusLabel(status: string | null | undefined): string {
  const s = (status || "").toLowerCase();
  if (s === "auto_matched") return "Auto matched";
  if (s === "needs_review") return "Needs review";
  if (s === "low_confidence") return "Low confidence";
  if (s === "no_candidate" || s === "no_match") return "No candidate";
  if (s === "confirmed") return "Confirmed";
  if (s === "rejected") return "Rejected";
  return status || "—";
}

function matchReasonDisplay(row: CategoryWorkspaceProduct): string {
  if (row.match_reason) return row.match_reason;
  const s = (row.match_status || "").toLowerCase();
  if (s === "no_candidate" || s === "no_match") return "No candidate found";
  if (s === "low_confidence") return "Low confidence match";
  if (s === "needs_review" && row.matched_by === "multiple_candidates") {
    return "Needs review — multiple candidates";
  }
  return "—";
}

function isReviewableMatchStatus(status: string | null | undefined): boolean {
  const s = (status || "").toLowerCase();
  return s === "needs_review" || s === "low_confidence";
}

function attributeEntries(row: CategoryWorkspaceProduct): [string, string][] {
  const attrs = row.listing_attributes ?? {};
  return Object.entries(attrs)
    .map(([k, v]) => [k, v == null ? "" : String(v)] as [string, string])
    .filter(([k, v]) => k.trim() && v.trim())
    .slice(0, 6);
}

type ProductListPage = { rows: Product[]; total: number; limit: number; offset: number; has_more: boolean };

type ApiDebugInfo = { apiBaseUrl: string; path: string; message: string };

function ApiDebugDetails({
  debug,
  expanded,
  onToggle,
}: {
  debug: ApiDebugInfo | null;
  expanded: boolean;
  onToggle: (open: boolean) => void;
}) {
  if (!debug) return null;
  return (
    <details
      className="api-debug-details"
      open={expanded}
      onToggle={(e) => onToggle((e.target as HTMLDetailsElement).open)}
    >
      <summary>Show API debug details</summary>
      <ul>
        <li>
          <strong>API base URL:</strong> <code>{debug.apiBaseUrl}</code>
        </li>
        <li>
          <strong>Endpoint:</strong> <code>{debug.path}</code>
        </li>
        <li>
          <strong>Full URL:</strong>{" "}
          <code>
            {debug.apiBaseUrl}
            {debug.path.startsWith("/") ? debug.path : `/${debug.path}`}
          </code>
        </li>
        <li>
          <strong>Error:</strong> {debug.message}
        </li>
      </ul>
      <p className="muted">
        Verify <a href={`${API_BASE_URL}/docs`}>{API_BASE_URL}/docs</a> opens in your browser.
      </p>
    </details>
  );
}

function scrapeMethodLabel(p: ScrapeTaskStatus): string {
  if ((p.current_phase ?? "").includes("bulk")) return "Catalog feed (bulk)";
  const browser =
    (p.adaptive_playwright_success ?? 0) + (p.playwright_fallback ?? 0) + (p.js_extract_success ?? 0);
  const http = (p.lightweight_success ?? 0) + (p.adaptive_fast_success ?? 0) + (p.occ_api_success ?? 0);
  if (browser === 0 && http === 0) return "—";
  if (browser > http) return "Browser (Playwright)";
  if (browser > 0) return "HTTP + browser fallback";
  return "HTTP";
}

/** Collapsed/expanded state for a workspace section, persisted per section key. */
function useCollapsedSection(storageKey: string): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(storageKey) === "1");
    } catch {
      /* storage unavailable */
    }
  }, [storageKey]);
  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      try {
        window.localStorage.setItem(storageKey, prev ? "0" : "1");
      } catch {
        /* storage unavailable */
      }
      return !prev;
    });
  }, [storageKey]);
  return [collapsed, toggle];
}

function CollapseToggle({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      className="section-collapse-toggle"
      onClick={onToggle}
      title={collapsed ? "Expand" : "Collapse"}
      aria-expanded={!collapsed}
    >
      {collapsed ? "▸" : "▾"}
    </button>
  );
}

function scrapeEta(p: ScrapeTaskStatus): string {
  const rate = p.products_per_minute ?? 0;
  const remaining = (p.total ?? 0) - (p.current ?? 0);
  if (remaining <= 0) return "done";
  if (rate <= 0) return "—";
  const minutes = remaining / rate;
  if (minutes < 1) return "< 1 min";
  if (minutes < 60) return `≈ ${Math.round(minutes)} min`;
  return `≈ ${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m`;
}

function ScrapeProgressPanel({
  progress,
  running,
  onStop,
  stopping,
}: {
  progress: ScrapeTaskStatus;
  running: boolean;
  onStop?: () => void;
  stopping?: boolean;
}) {
  const total = progress.total ?? 0;
  const current = progress.current ?? 0;
  const isBulk = (progress.current_phase ?? "").includes("bulk");
  const fetchingCatalog = total === 0 && isBulk;
  const pagesTotal = progress.pages_total ?? 0;
  const pagesScanned = progress.pages_scanned ?? 0;
  const pct = fetchingCatalog
    ? pagesTotal > 0
      ? Math.min(100, (100 * pagesScanned) / pagesTotal)
      : 0
    : total > 0
      ? Math.min(100, (100 * current) / total)
      : 0;
  const catalogEta = etaLabel(pagesTotal - pagesScanned, progress.pages_per_minute ?? 0);
  const [collapsed, toggleCollapsed] = useCollapsedSection("pm_ui_collapse_scrape_progress");

  const tiles: { label: string; value: string; tone?: "warn" }[] = fetchingCatalog
    ? [
        { label: "Products found", value: (progress.product_urls_found ?? 0).toLocaleString() },
        {
          label: "Catalog size",
          value: (progress.catalog_total ?? 0) > 0 ? (progress.catalog_total ?? 0).toLocaleString() : "…",
        },
        {
          label: "Catalog pages",
          value: pagesTotal > 0 ? `${pagesScanned.toLocaleString()} / ${pagesTotal.toLocaleString()}` : pagesScanned.toLocaleString(),
        },
        { label: "Time left", value: pagesTotal > 0 ? catalogEta : "—" },
      ]
    : isBulk
      ? [
          { label: "Success rate", value: `${(progress.success_pct ?? 0).toLocaleString()}%` },
          {
            label: "Errors",
            value: (progress.failed ?? 0).toLocaleString(),
            tone: (progress.failed ?? 0) > 0 ? "warn" : undefined,
          },
          { label: "Skipped", value: (progress.skipped ?? 0).toLocaleString() },
          { label: "Per minute", value: (progress.products_per_minute ?? 0).toLocaleString() },
          { label: "Time left", value: scrapeEta(progress) },
        ]
      : [
          { label: "Success rate", value: `${(progress.success_pct ?? 0).toLocaleString()}%` },
          {
            label: "Errors",
            value: (progress.failed ?? 0).toLocaleString(),
            tone: (progress.failed ?? 0) > 0 ? "warn" : undefined,
          },
          { label: "Skipped", value: (progress.skipped ?? 0).toLocaleString() },
          { label: "Per minute", value: (progress.products_per_minute ?? 0).toLocaleString() },
          { label: "Time left", value: scrapeEta(progress) },
          { label: "Avg / product", value: `${(progress.avg_scrape_ms ?? 0).toLocaleString()} ms` },
          { label: "Concurrency", value: String(progress.current_concurrency ?? 0) },
        ];

  const failReasons = Object.entries(progress.failed_by_reason ?? {});

  return (
    <div className="card match-all-progress" style={{ marginBottom: "1rem" }}>
      <div className="stats-header">
        <h3 style={{ margin: 0, fontSize: "1rem" }}>
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} />
          {running
            ? stopping
              ? "Stopping…"
              : "Scraping…"
            : progress.current_phase === "cancelled"
              ? "Scraping stopped"
              : progress.current_phase === "blocked"
                ? "Scraping blocked — site captcha"
                : "Scraping finished"}
        </h3>
        <span className="muted" style={{ fontSize: "0.82rem" }}>
          {fetchingCatalog
            ? pagesTotal > 0
              ? `Downloading the full catalog… ${Math.round(pct)}%`
              : "Downloading the full catalog… (counting products)"
            : isBulk
              ? `Saving ${current.toLocaleString()} / ${total.toLocaleString()} products`
              : `${current.toLocaleString()} / ${total.toLocaleString()} products`}
          {" · "}
          <span className="badge badge-neutral">{scrapeMethodLabel(progress)}</span>
          {running && onStop ? (
            <>
              {" "}
              <button
                type="button"
                onClick={onStop}
                disabled={stopping}
                style={{ fontSize: "0.78rem", padding: "0.15rem 0.55rem", marginLeft: "0.5rem" }}
                title="Finishes the in-flight chunk, saves everything scraped so far, then stops"
              >
                {stopping ? "Stopping…" : "Stop"}
              </button>
            </>
          ) : null}
        </span>
      </div>
      <div className="stat-bar" style={{ marginTop: "0.15rem" }}>
        <div
          className={fetchingCatalog && pagesTotal === 0 ? "stat-bar-fill stat-bar-indeterminate" : "stat-bar-fill"}
          style={{ width: fetchingCatalog && pagesTotal === 0 ? "40%" : `${pct}%` }}
        />
      </div>
      {!collapsed ? (
        <>
          <div className="stats-grid" style={{ marginTop: "0.35rem" }}>
            {tiles.map((t) => (
              <div key={t.label} className="stat-tile">
                <span className={t.tone === "warn" ? "stat-value stat-value-warn" : "stat-value"}>{t.value}</span>
                <span className="stat-label">{t.label}</span>
              </div>
            ))}
          </div>
          {failReasons.length > 0 ? (
            <div className="stats-chip-row">
              {failReasons.map(([reason, count]) => (
                <span key={reason} className="badge badge-danger">
                  {reason.replace(/_/g, " ")} · {Number(count).toLocaleString()}
                </span>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function etaLabel(remaining: number, ratePerMin: number): string {
  if (remaining <= 0) return "done";
  if (ratePerMin <= 0) return "—";
  const minutes = remaining / ratePerMin;
  if (minutes < 1) return "< 1 min";
  if (minutes < 60) return `≈ ${Math.round(minutes)} min`;
  return `≈ ${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m`;
}

function MatchProgressPanel({ progress, running }: { progress: MatchTaskStatus; running: boolean }) {
  const total = progress.total ?? 0;
  const current = progress.current ?? 0;
  const pct = total > 0 ? Math.min(100, (100 * current) / total) : 0;
  const rate = progress.products_per_minute ?? 0;
  const noCandidate = progress.no_candidate ?? progress.no_match ?? 0;
  const [collapsed, toggleCollapsed] = useCollapsedSection("pm_ui_collapse_match_progress");

  const tiles: { label: string; value: string; tone?: "warn" }[] = [
    { label: "Auto matched", value: (progress.matched ?? 0).toLocaleString() },
    { label: "Needs review", value: (progress.needs_review ?? 0).toLocaleString() },
    { label: "Low confidence", value: (progress.low_confidence ?? 0).toLocaleString() },
    { label: "No candidate", value: noCandidate.toLocaleString() },
    { label: "Skipped", value: (progress.skipped ?? 0).toLocaleString() },
    { label: "Per minute", value: rate.toLocaleString() },
    { label: "Time left", value: etaLabel(total - current, rate) },
  ];
  if ((progress.failed ?? 0) > 0) {
    tiles.push({ label: "Failed", value: (progress.failed ?? 0).toLocaleString(), tone: "warn" });
  }

  const skipReasons = Object.entries(progress.skipped_by_reason ?? {});

  return (
    <div className="card match-all-progress" style={{ marginBottom: "1rem" }}>
      <div className="stats-header">
        <h3 style={{ margin: 0, fontSize: "1rem" }}>
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} />
          {running ? "Matching…" : "Matching finished"}
        </h3>
        <span className="muted" style={{ fontSize: "0.82rem" }}>
          {`${current.toLocaleString()} / ${total.toLocaleString()} products`}
          {collapsed ? ` · ${etaLabel(total - current, rate)}` : ""}
          {progress.current_phase ? (
            <>
              {" · "}
              <span className="badge badge-neutral">{progress.current_phase}</span>
            </>
          ) : null}
        </span>
      </div>
      <div className="stat-bar" style={{ marginTop: "0.15rem" }}>
        <div className="stat-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      {!collapsed ? (
        <>
          <div className="stats-grid" style={{ marginTop: "0.35rem" }}>
            {tiles.map((t) => (
              <div key={t.label} className="stat-tile">
                <span className={t.tone === "warn" ? "stat-value stat-value-warn" : "stat-value"}>{t.value}</span>
                <span className="stat-label">{t.label}</span>
              </div>
            ))}
          </div>
          {skipReasons.length > 0 ? (
            <div className="stats-chip-row">
              {skipReasons.map(([reason, count]) => (
                <span key={reason} className="badge badge-neutral">
                  {reason.replace(/_/g, " ")} · {Number(count).toLocaleString()}
                </span>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function DiscoveryProgressPanel({
  progress,
  running,
  onStop,
  stopping,
}: {
  progress: DiscoveryTaskStatus;
  running: boolean;
  onStop?: () => void;
  stopping?: boolean;
}) {
  const total = progress.total ?? 0;
  const current = progress.current ?? 0;
  const hasTotal = total > 0;
  const pct = hasTotal ? Math.min(100, (100 * current) / total) : 0;
  const [collapsed, toggleCollapsed] = useCollapsedSection("pm_ui_collapse_discovery_progress");

  const tiles: { label: string; value: string; tone?: "warn" }[] = [
    { label: "Found", value: (progress.product_urls_found ?? 0).toLocaleString() },
    { label: "New", value: (progress.new_urls_found ?? 0).toLocaleString() },
    { label: "Saved", value: (progress.created ?? 0).toLocaleString() },
    { label: "Skipped existing", value: (progress.skipped_existing ?? 0).toLocaleString() },
    { label: "Sitemaps", value: (progress.sitemap_files_checked ?? 0).toLocaleString() },
    { label: "Pages scanned", value: (progress.pages_scanned ?? 0).toLocaleString() },
  ];
  if ((progress.categories_updated ?? 0) > 0) {
    tiles.push({
      label: "Categories updated",
      value: (progress.categories_updated ?? 0).toLocaleString(),
    });
  }

  return (
    <div className="card match-all-progress" style={{ marginBottom: "1rem" }}>
      <div className="stats-header">
        <h3 style={{ margin: 0, fontSize: "1rem" }}>
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} />
          {running
            ? stopping
              ? "Stopping…"
              : "Finding URLs…"
            : progress.current_phase === "cancelled"
              ? "Discovery stopped"
              : "Discovery finished"}
        </h3>
        <span className="muted" style={{ fontSize: "0.82rem" }}>
          {discoveryPhaseLabel(progress.current_phase)}
          {hasTotal ? ` — ${current.toLocaleString()} / ${total.toLocaleString()}` : ""}
          {running && onStop ? (
            <>
              {" "}
              <button
                type="button"
                onClick={onStop}
                disabled={stopping}
                style={{ fontSize: "0.78rem", padding: "0.15rem 0.55rem", marginLeft: "0.5rem" }}
                title="Stops the current method and saves every URL found so far"
              >
                {stopping ? "Stopping…" : "Stop"}
              </button>
            </>
          ) : null}
        </span>
      </div>
      <div className="stat-bar" style={{ marginTop: "0.15rem" }}>
        <div
          className={hasTotal ? "stat-bar-fill" : "stat-bar-fill stat-bar-indeterminate"}
          style={{ width: hasTotal ? `${pct}%` : "40%" }}
        />
      </div>
      {collapsed ? null : (
      <>
      <div className="stats-grid" style={{ marginTop: "0.35rem" }}>
        {tiles.map((t) => (
          <div key={t.label} className="stat-tile">
            <span className={t.tone === "warn" ? "stat-value stat-value-warn" : "stat-value"}>{t.value}</span>
            <span className="stat-label">{t.label}</span>
          </div>
        ))}
      </div>
      {progress.probe?.best_method ? (
        <p className="muted" style={{ margin: "0.35rem 0 0", fontSize: "0.82rem" }}>
          Probe: {progress.probe.platform ? `platform ${progress.probe.platform}, ` : ""}
          best method {DISCOVERY_METHOD_LABEL[progress.probe.best_method] ?? progress.probe.best_method}
          {progress.probe.method_reasons?.[progress.probe.best_method]
            ? ` — ${progress.probe.method_reasons[progress.probe.best_method]}`
            : ""}
          {progress.probe.blocked ? " (site behind anti-bot challenge)" : ""}
        </p>
      ) : null}
      {progress.discovery_methods?.length ? (
        <div className="discovery-method-log">
          {progress.discovery_methods.map((method) => (
            <div className="discovery-method-row" key={method.method}>
              <strong>{method.label}</strong>
              <span>{method.status}</span>
              <span>found {(method.found ?? 0).toLocaleString()}</span>
              <span>added {(method.added ?? 0).toLocaleString()}</span>
              <span>duplicates {(method.skipped_duplicate ?? 0).toLocaleString()}</span>
              {method.blocked ? <span>blocked: {method.block_reason ?? "access denied"}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
      </>
      )}
    </div>
  );
}

type CompetitorStats = {
  competitor_id: string;
  total_urls: number;
  scraped: number;
  with_price: number;
  failed: number;
  never_scraped: number;
  dead_urls: number;
  matched: number;
  auto_matched?: number;
  needs_review?: number;
  low_confidence?: number;
  coverage_pct: number;
  last_scraped_at: string | null;
  last_discovered_at: string | null;
  discovery_sources: { source: string; count: number }[];
  scrape_method: string;
};

const SCRAPE_TASK_STORAGE_KEY = "pm_scrape_all_task";

const DISCOVERY_SOURCE_LABEL: Record<string, string> = {
  sitemap: "Sitemap",
  full_sitemap: "Full sitemap",
  douglas_graphql_bulk: "Catalog feed",
  magento_graphql_bulk: "Catalog feed",
  category_pagination: "Category pages",
  external_search: "External search",
  site_search: "Site search",
  dynamic_endpoints: "Product APIs",
  manual: "Manual",
};

function fmtStatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function CompetitorStatsDashboard({
  competitorId,
  competitorName,
  refreshKey,
}: {
  competitorId: string;
  competitorName: string;
  refreshKey: number;
}) {
  const [stats, setStats] = useState<CompetitorStats | null>(null);
  const [statsError, setStatsError] = useState(false);
  const [collapsed, toggleCollapsed] = useCollapsedSection("pm_ui_collapse_overview");

  useEffect(() => {
    let cancelled = false;
    setStatsError(false);
    api
      .get<CompetitorStats>(`/competitors/${competitorId}/stats`)
      .then((s) => {
        if (!cancelled) setStats(s);
      })
      .catch(() => {
        if (!cancelled) setStatsError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [competitorId, refreshKey]);

  if (statsError) return null;
  if (!stats || stats.competitor_id !== competitorId) {
    return (
      <div className="card stats-card" style={{ marginBottom: "1rem" }}>
        <span className="workspace-skeleton-cell" style={{ width: "40%" }} />
      </div>
    );
  }

  const coveragePct = Number.isFinite(stats.coverage_pct)
    ? Math.max(0, Math.min(100, stats.coverage_pct))
    : 0;
  const tiles: { label: string; value: string; tone: string; icon: string }[] = [
    { label: "Product URLs", value: stats.total_urls.toLocaleString(), tone: "purple", icon: "URL" },
    { label: "With price", value: stats.with_price.toLocaleString(), tone: "blue", icon: "€" },
    { label: "Not scraped yet", value: stats.never_scraped.toLocaleString(), tone: "violet", icon: "…" },
    { label: "Failed", value: stats.failed.toLocaleString(), tone: "danger", icon: "!" },
    { label: "Matched to catalog", value: stats.matched.toLocaleString(), tone: "cyan", icon: "✓" },
    { label: "Auto matched", value: (stats.auto_matched ?? 0).toLocaleString(), tone: "green", icon: "A" },
    { label: "Needs review", value: (stats.needs_review ?? 0).toLocaleString(), tone: "warn", icon: "?" },
    { label: "Low confidence", value: (stats.low_confidence ?? 0).toLocaleString(), tone: "orange", icon: "!" },
  ];

  return (
    <div className="card stats-card">
      <div className="stats-header">
        <h3>
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} />
          {competitorName} overview
        </h3>
        <span className="muted" style={{ fontSize: "0.8rem" }}>
          {collapsed
            ? `${stats.total_urls.toLocaleString()} URLs · ${stats.with_price.toLocaleString()} with price · ${coveragePct.toFixed(1)}% coverage · `
            : ""}
          URLs found {fmtStatDate(stats.last_discovered_at)} · prices updated {fmtStatDate(stats.last_scraped_at)}
        </span>
      </div>

      {collapsed ? null : (
      <>
      <div className="stats-grid">
        {tiles.map((t) => (
          <div key={t.label} className={`stat-tile stat-tile-${t.tone}`}>
            <span className="stat-icon" aria-hidden>{t.icon}</span>
            <span className={t.tone === "danger" && stats.failed > 0 ? "stat-value stat-value-warn" : "stat-value"}>{t.value}</span>
            <span className="stat-label">{t.label}</span>
          </div>
        ))}
        <div className="stat-tile stat-tile-wide stat-tile-cyan">
          <span className="stat-icon" aria-hidden>%</span>
          <span className="stat-value">{coveragePct.toFixed(1)}%</span>
          <span className="stat-label">Price coverage</span>
          <div className="stat-bar" role="img" aria-label={`Price coverage ${coveragePct.toFixed(1)}%`}>
            <div className="stat-bar-fill" style={{ width: `${coveragePct}%` }} />
          </div>
        </div>
      </div>

      <div className="stats-methods">
        <div className="stats-method-row">
          <span className="stat-label">URL discovery</span>
          <span className="stats-chip-row">
            {stats.discovery_sources.slice(0, 4).map((s) => (
              <span key={s.source} className="badge badge-info">
                {DISCOVERY_SOURCE_LABEL[s.source] ?? s.source} · {s.count.toLocaleString()}
              </span>
            ))}
            {stats.discovery_sources.length === 0 ? <span className="muted">—</span> : null}
          </span>
        </div>
        <div className="stats-method-row">
          <span className="stat-label">Price scraping</span>
          <span className="badge badge-neutral">{stats.scrape_method}</span>
        </div>
      </div>
      </>
      )}
    </div>
  );
}

function findCategoryPathInTree(
  nodes: CategoryTreeNode[],
  categoryId: string,
  prefix: string[] = [],
): string[] | null {
  for (const n of nodes) {
    const path = [...prefix, n.name];
    if (n.id === categoryId) return path;
    const child = findCategoryPathInTree(n.children, categoryId, path);
    if (child) return child;
  }
  return null;
}

function TreeNodes(props: {
  nodes: CategoryTreeNode[];
  selectedId: string | null;
  onPick: (id: string, pathNames: string[]) => void;
  depth: number;
  pathPrefix?: string[];
}) {
  const { nodes, selectedId, onPick, depth, pathPrefix = [] } = props;
  return (
    <ul className={`cat-tree cat-tree-depth-${Math.min(depth, 3)}`}>
      {nodes.map((n) => {
        const pathNames = [...pathPrefix, n.name];
        return (
          <li key={n.id}>
            <button
              type="button"
              className={selectedId === n.id ? "cat-tree-node cat-tree-node-active" : "cat-tree-node"}
              onClick={() => onPick(n.id, pathNames)}
            >
              <span className="cat-tree-name">{n.name}</span>
              <span className="muted cat-tree-count">{n.product_count}</span>
            </button>
            {n.children?.length ? (
              <TreeNodes
                nodes={n.children}
                selectedId={selectedId}
                onPick={onPick}
                depth={depth + 1}
                pathPrefix={pathNames}
              />
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export default function CompetitorsPage() {
  const { state: healthState, healthy } = useApiHealth();

  const [tree, setTree] = useState<CompetitorTreeItem[]>([]);
  const [flatCompetitors, setFlatCompetitors] = useState<Competitor[]>([]);
  const [treeError, setTreeError] = useState<string | null>(null);
  const [treeStale, setTreeStale] = useState(false);
  const [treeRefreshing, setTreeRefreshing] = useState(false);

  const [blockingError, setBlockingError] = useState<string | null>(null);
  const [apiDebug, setApiDebug] = useState<ApiDebugInfo | null>(null);
  const [showApiDebug, setShowApiDebug] = useState(false);

  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [fullDiscoveryDiag, setFullDiscoveryDiag] = useState<FullDiscoveryResult | null>(null);

  const treeAbortRef = useRef<AbortController | null>(null);
  const hasLoadedTreeRef = useRef(false);

  const [selectedCompetitorId, setSelectedCompetitorId] = useState<string | null>(null);
  const [selectedCategoryId, setSelectedCategoryId] = useState<string | null>(null);
  const [selectedCategoryPath, setSelectedCategoryPath] = useState<string[]>([]);

  const [workspace, setWorkspace] = useState<CategoryWorkspaceProduct[]>([]);
  const [workspaceTotal, setWorkspaceTotal] = useState(0);
  const [workspacePageSize, setWorkspacePageSize] = useState(DEFAULT_WORKSPACE_PAGE_SIZE);
  const [workspaceOffset, setWorkspaceOffset] = useState(0);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [catalogProducts, setCatalogProducts] = useState<Product[]>([]);

  const [busy, setBusy] = useState<string | null>(null);
  const [scrapingId, setScrapingId] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  const [findForId, setFindForId] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<MatchCandidate[]>([]);

  const [matchAllTaskId, setMatchAllTaskId] = useState<string | null>(null);
  const [matchAllProgress, setMatchAllProgress] = useState<MatchTaskStatus | null>(null);
  const [scrapeAllTaskId, setScrapeAllTaskId] = useState<string | null>(null);
  const [scrapeAllProgress, setScrapeAllProgress] = useState<ScrapeTaskStatus | null>(null);
  const [scrapeStopping, setScrapeStopping] = useState(false);
  const [discoveryStopping, setDiscoveryStopping] = useState(false);
  const [discoveryAllTaskId, setDiscoveryAllTaskId] = useState<string | null>(null);
  const [discoveryAllProgress, setDiscoveryAllProgress] = useState<DiscoveryTaskStatus | null>(null);
  const [autoDiscovery, setAutoDiscovery] = useState(true);
  // Quick mode: run only on-site search with the given terms, even when Auto
  // is on — fast, direct lookups without a full-site crawl.
  const [quickSiteSearch, setQuickSiteSearch] = useState(false);
  const [selectedDiscoveryMethods, setSelectedDiscoveryMethods] = useState<DiscoveryMethod[]>([
    "sitemap",
    "category_pagination",
  ]);
  const [discoverySeedTerms, setDiscoverySeedTerms] = useState("");
  const [concurrencyDraft, setConcurrencyDraft] = useState("");
  // Scrape all: skip listings that already have a price (only_missing).
  const [scrapeOnlyMissing, setScrapeOnlyMissing] = useState(false);
  const [workspaceScrapeFilter, setWorkspaceScrapeFilter] = useState<WorkspaceScrapeFilter>("all");
  const [workspaceMatchFilter, setWorkspaceMatchFilter] = useState<WorkspaceMatchFilter>("all");
  const [workspacePinnedColumns, setWorkspacePinnedColumns] = useState<WorkspaceColumnKey[]>([]);
  const [workspaceHiddenColumns, setWorkspaceHiddenColumns] = useState<WorkspaceColumnKey[]>([]);
  const [openWorkspaceMenu, setOpenWorkspaceMenu] = useState<WorkspacePopoverMenu | null>(null);
  const [workspaceSearch, setWorkspaceSearch] = useState("");
  const [retailerSearch, setRetailerSearch] = useState("");
  const [chooseCandidates, setChooseCandidates] = useState<MatchCandidate[]>([]);

  const matchAllRunning =
    busy === "match-all" ||
    (matchAllTaskId != null && (matchAllProgress == null || !matchAllProgress.ready));
  const scrapeAllRunning =
    busy === "scrape-all" ||
    (scrapeAllTaskId != null && (scrapeAllProgress == null || !scrapeAllProgress.ready));
  const discoveryAllRunning =
    busy === "full-discovery" ||
    (discoveryAllTaskId != null && (discoveryAllProgress == null || !discoveryAllProgress.ready));
  const siteSearchOnlyNeedsTerm =
    (quickSiteSearch ||
      (!autoDiscovery &&
        selectedDiscoveryMethods.length === 1 &&
        selectedDiscoveryMethods.includes("site_search"))) &&
    discoverySeedTerms.trim().length === 0;

  const [chooseForId, setChooseForId] = useState<string | null>(null);
  const [chooseRow, setChooseRow] = useState<CategoryWorkspaceProduct | null>(null);
  const [chooseLoading, setChooseLoading] = useState(false);
  const [chooseError, setChooseError] = useState<string | null>(null);
  const [productQuery, setProductQuery] = useState("");

  // Add competitor
  const [cName, setCName] = useState("");
  const [cDomain, setCDomain] = useState("");
  const [cCountry, setCCountry] = useState("");
  const [cCurrency, setCCurrency] = useState("BGN");

  // Add single product URL (isolated from tree selection refresh)
  const [addUrlCompetitorId, setAddUrlCompetitorId] = useState("");
  const [singleProductUrl, setSingleProductUrl] = useState("");
  const [addUrlStatus, setAddUrlStatus] = useState<string | null>(null);
  const [addUrlError, setAddUrlError] = useState<string | null>(null);
  const [scrapeAfterAdd, setScrapeAfterAdd] = useState(false);
  const addUrlCompetitorIdRef = useRef("");

  const recordEndpointFailure = useCallback(
    (err: unknown, path: string, opts?: { allowBlocking?: boolean; hasCachedData?: boolean }) => {
      if (isApiAbortError(err)) return;

      const message = getSectionErrorMessage(err);
      if (isApiError(err)) {
        setApiDebug({ apiBaseUrl: err.apiBaseUrl, path: err.path, message: err.message });
      } else {
        setApiDebug({ apiBaseUrl: API_BASE_URL, path, message });
      }

      const shouldBlock =
        opts?.allowBlocking !== false &&
        healthState === "error" &&
        !opts?.hasCachedData &&
        !healthy;

      if (shouldBlock) {
        setBlockingError(isApiError(err) ? err.message : message);
      }
    },
    [healthState, healthy],
  );

  const competitorOptions = useMemo(() => {
    if (flatCompetitors.length > 0) {
      return flatCompetitors.map((c) => ({ id: c.id, name: c.name, domain: c.domain }));
    }
    return tree.map((c) => ({ id: c.id, name: c.name, domain: c.domain }));
  }, [flatCompetitors, tree]);

  const groupedRetailers = useMemo(() => {
    const query = retailerSearch.trim().toLowerCase();
    const filtered = query
      ? tree.filter((c) =>
          [c.name, c.domain, c.country ?? ""].some((value) =>
            value.toLowerCase().includes(query),
          ),
        )
      : tree;
    const groups = new Map<string, CompetitorTreeItem[]>();
    for (const competitor of filtered) {
      const country = inferRetailerCountry(competitor);
      groups.set(country, [...(groups.get(country) ?? []), competitor]);
    }
    return [...groups.entries()]
      .map(([country, retailers]) => ({
        country,
        label: countryGroupLabel(country),
        retailers: retailers.sort((a, b) => a.name.localeCompare(b.name)),
      }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [retailerSearch, tree]);

  const restoreAddUrlCompetitorSelection = useCallback(
    (options: { id: string }[]) => {
      if (
        addUrlCompetitorIdRef.current &&
        options.some((o) => o.id === addUrlCompetitorIdRef.current)
      ) {
        setAddUrlCompetitorId(addUrlCompetitorIdRef.current);
        return;
      }
      if (selectedCompetitorId && options.some((o) => o.id === selectedCompetitorId)) {
        setAddUrlCompetitorId(selectedCompetitorId);
        addUrlCompetitorIdRef.current = selectedCompetitorId;
        return;
      }
      if (options.length === 1) {
        setAddUrlCompetitorId(options[0].id);
        addUrlCompetitorIdRef.current = options[0].id;
      }
    },
    [selectedCompetitorId],
  );

  const [deletingCompetitorId, setDeletingCompetitorId] = useState<string | null>(null);

  const refreshTreeAndLists = useCallback(
    async (signal?: AbortSignal) => {
      setTreeRefreshing(true);
      setTreeError(null);
      let treeOk = false;
      let compsOk = false;
      let latestComps: Competitor[] = flatCompetitors;

      try {
        const tr = await api.get<CompetitorTreeItem[]>("/competitors/tree", { signal });
        setTree(tr);
        setTreeStale(false);
        setTreeError(null);
        hasLoadedTreeRef.current = true;
        treeOk = true;
      } catch (err) {
        if (isApiAbortError(err)) return;
        const hasCached = hasLoadedTreeRef.current;
        setTreeError(
          hasCached
            ? "Could not refresh catalog. Using last loaded data."
            : getSectionErrorMessage(err),
        );
        if (hasCached) setTreeStale(true);
        recordEndpointFailure(err, "/competitors/tree", { hasCachedData: hasCached });
      }

      try {
        const comps = await api.get<Competitor[]>("/competitors", { signal });
        setFlatCompetitors(comps);
        latestComps = comps;
        compsOk = true;
      } catch (err) {
        if (isApiAbortError(err)) return;
        // Keep existing flatCompetitors — do not clear dropdown
      }

      if (treeOk || compsOk) {
        setBlockingError(null);
      }

      const options =
        latestComps.length > 0
          ? latestComps.map((c) => ({ id: c.id }))
          : tree.map((c) => ({ id: c.id }));
      if (options.length > 0) {
        restoreAddUrlCompetitorSelection(options);
      }

      setTreeRefreshing(false);
    },
    [flatCompetitors, recordEndpointFailure, restoreAddUrlCompetitorSelection, tree],
  );

  const refreshTreeRef = useRef(refreshTreeAndLists);
  refreshTreeRef.current = refreshTreeAndLists;

  const fetchWorkspacePage = useCallback(
    async (opts?: {
      offset?: number;
      limit?: number;
      signal?: AbortSignal;
      categoryId?: string | null;
      competitorId?: string | null;
      scrapedFilter?: WorkspaceScrapeFilter;
      matchStatus?: WorkspaceMatchFilter;
      search?: string;
    }) => {
      const limit = opts?.limit ?? workspacePageSize;
      const offset = opts?.offset ?? workspaceOffset;
      const scraped = opts?.scrapedFilter ?? workspaceScrapeFilter;
      const matchStatus = opts?.matchStatus ?? workspaceMatchFilter;
      const search = opts?.search ?? workspaceSearch;
      const query = workspaceProductsQuery(limit, offset, { scraped, matchStatus, search });
      const categoryId = opts?.categoryId !== undefined ? opts.categoryId : selectedCategoryId;
      const competitorId = opts?.competitorId !== undefined ? opts.competitorId : selectedCompetitorId;

      if (!categoryId && !competitorId) {
        setWorkspace([]);
        setWorkspaceTotal(0);
        setWorkspaceOffset(0);
        return null;
      }

      setWorkspaceLoading(true);
      setWorkspaceError(null);
      try {
        const page = categoryId
          ? await api.get<CategoryWorkspacePage>(
              `/competitor-categories/${categoryId}/products?${query}`,
              { signal: opts?.signal },
            )
          : await api.get<CategoryWorkspacePage>(
              `/competitors/${competitorId}/products?${query}`,
              { signal: opts?.signal },
            );

        setWorkspace(page.rows);
        setWorkspaceTotal(page.total);
        setWorkspacePageSize(page.limit);
        setWorkspaceOffset(page.offset);
        return page;
      } catch (err) {
        if (isApiAbortError(err)) return null;
        const msg = getSectionErrorMessage(err);
        setWorkspaceError(msg);
        const path = categoryId
          ? `/competitor-categories/${categoryId}/products`
          : `/competitors/${competitorId}/products`;
        recordEndpointFailure(err, path, {
          allowBlocking: false,
          hasCachedData: workspace.length > 0,
        });
        return null;
      } finally {
        setWorkspaceLoading(false);
      }
    },
    [
      selectedCategoryId,
      selectedCompetitorId,
      workspacePageSize,
      workspaceOffset,
      workspace.length,
      workspaceScrapeFilter,
      workspaceMatchFilter,
      workspaceSearch,
      recordEndpointFailure,
    ],
  );
  const fetchWorkspacePageRef = useRef(fetchWorkspacePage);
  fetchWorkspacePageRef.current = fetchWorkspacePage;

  const refreshWorkspace = useCallback(
    async (opts?: { offset?: number; limit?: number }) => {
      return fetchWorkspacePage(opts);
    },
    [fetchWorkspacePage],
  );

  const pickCategory = useCallback(
    (categoryId: string, pathNames: string[]) => {
      setSelectedCategoryId(categoryId);
      setSelectedCategoryPath(pathNames);
      setWorkspaceOffset(0);
      void fetchWorkspacePage({ offset: 0, categoryId, competitorId: null });
    },
    [fetchWorkspacePage],
  );

  const workspaceRangeStart = workspaceTotal === 0 ? 0 : workspaceOffset + 1;
  const workspaceRangeEnd = Math.min(workspaceOffset + workspace.length, workspaceTotal);
  const canGoPrev = workspaceOffset > 0;
  const canGoNext = workspaceOffset + workspace.length < workspaceTotal;
  const hasWorkspaceFilters =
    workspaceScrapeFilter !== "all" ||
    workspaceMatchFilter !== "all" ||
    workspaceSearch.trim().length > 0;
  const workspaceEmptyFilterLabels = [
    workspaceScrapeFilterLabel(workspaceScrapeFilter),
    workspaceMatchFilterLabel(workspaceMatchFilter),
    workspaceSearch.trim() ? `search “${workspaceSearch.trim()}”` : "",
  ].filter(Boolean);
  const workspaceEmptyFilterMessage =
    workspaceEmptyFilterLabels.length > 0
      ? `No ${workspaceEmptyFilterLabels.join(" + ")} products in this tracked catalog.`
      : "No products match the current filters.";
  const workspacePinnedColumnSet = useMemo(
    () => new Set<WorkspaceColumnKey>(workspacePinnedColumns),
    [workspacePinnedColumns],
  );
  const workspaceHiddenColumnSet = useMemo(
    () => new Set<WorkspaceColumnKey>(workspaceHiddenColumns),
    [workspaceHiddenColumns],
  );
  const visibleWorkspaceColumns = useMemo(
    () => WORKSPACE_COLUMNS.filter((column) => !workspaceHiddenColumnSet.has(column.key)),
    [workspaceHiddenColumnSet],
  );
  const workspacePinnedColumnLeft = useMemo(() => {
    const left = new Map<WorkspaceColumnKey, number>();
    let offset = 0;
    for (const column of visibleWorkspaceColumns) {
      if (!workspacePinnedColumnSet.has(column.key)) continue;
      left.set(column.key, offset);
      offset += column.pinWidth;
    }
    return left;
  }, [visibleWorkspaceColumns, workspacePinnedColumnSet]);

  function toggleWorkspacePinnedColumn(column: WorkspaceColumnKey) {
    setWorkspacePinnedColumns((current) =>
      current.includes(column)
        ? current.filter((item) => item !== column)
        : [...current, column],
    );
  }

  function toggleWorkspaceHiddenColumn(column: WorkspaceColumnKey) {
    setWorkspaceHiddenColumns((current) => {
      const next = current.includes(column)
        ? current.filter((item) => item !== column)
        : [...current, column];
      if (!current.includes(column)) {
        setWorkspacePinnedColumns((pinned) => pinned.filter((item) => item !== column));
      }
      return next;
    });
  }

  function workspacePinClass(column: WorkspaceColumnKey, className?: string): string | undefined {
    if (!workspacePinnedColumnSet.has(column)) return className;
    return ["workspace-pinned-cell", className].filter(Boolean).join(" ");
  }

  function workspacePinStyle(
    column: WorkspaceColumnKey,
    style?: CSSProperties,
    area: "body" | "header" = "body",
  ): CSSProperties | undefined {
    if (!workspacePinnedColumnSet.has(column)) return style;
    const width = WORKSPACE_COLUMNS.find((item) => item.key === column)?.pinWidth;
    return {
      ...style,
      position: "sticky",
      left: workspacePinnedColumnLeft.get(column) ?? 0,
      zIndex: area === "header" ? 12 : 3,
      minWidth: width,
      width: style?.width ?? width,
    };
  }

  function renderWorkspaceCell(row: CategoryWorkspaceProduct, column: WorkspaceColumnKey): ReactNode {
    switch (column) {
      case "image":
        return row.image_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={row.image_url} alt="" className="thumb-48" loading="lazy" decoding="async" />
        ) : (
          <div className="thumb-48 thumb-placeholder" />
        );
      case "title":
        return row.title ?? "—";
      case "category_path":
        return row.category_path?.length ? row.category_path.join(" › ") : "—";
      case "url":
        return (
          <a
            href={row.url}
            target="_blank"
            rel="noopener noreferrer"
            className="table-link"
            title={row.url}
            style={{ maxWidth: 190 }}
          >
            {row.url.replace(/^https?:\/\/(www\.)?/, "")}
          </a>
        );
      case "brand":
        return row.listing_brand ?? "—";
      case "code":
        return row.listing_sku ?? "—";
      case "ean":
        return row.listing_ean ?? "—";
      case "mfr_model":
        return [row.listing_manufacturer_code, row.listing_model].filter(Boolean).join(" / ") || "—";
      case "size":
        return row.listing_size ?? "—";
      case "color":
        return row.listing_color ?? "—";
      case "product_price":
        return fmtMoney(row.matched_own_price);
      case "final_eur":
        return fmtMoney(row.latest_price);
      case "regular_eur":
        return fmtMoney(row.regular_price);
      case "promo_eur":
        return fmtMoney(row.promo_price);
      case "old_list_eur":
        return fmtMoney(row.old_price);
      case "currency":
        return row.currency || "—";
      case "availability":
        return <span className="badge badge-neutral">{row.availability ?? "—"}</span>;
      case "offered_by":
        return row.offered_by ?? "—";
      case "delivered_by":
        return row.delivered_by ?? "—";
      case "last_checked":
        return scrapingId === row.competitor_product_id ? (
          <span className="muted">Scraping…</span>
        ) : (
          fmtLastChecked(row.last_checked_at ?? row.last_seen_at)
        );
      case "matched_sku":
        return row.matched_sku ?? "—";
      case "matched_name":
        return row.matched_product_name ?? "—";
      case "score":
        return row.match_score ?? "—";
      case "match":
        return row.match_method ? (
          <span className="workspace-match-method" title={row.match_method}>
            {row.match_method}
          </span>
        ) : (
          "—"
        );
      case "status":
        return <span className={statusBadgeClass(row.match_status)}>{matchStatusLabel(row.match_status)}</span>;
      case "reason":
        return matchReasonDisplay(row);
      case "actions":
        return (
          <div className="workspace-row-actions">
            <button
              type="button"
              disabled={busy === row.competitor_product_id || scrapingId === row.competitor_product_id}
              onClick={() => scrapeListing(row.competitor_product_id)}
            >
              Scrape
            </button>
            <button
              type="button"
              disabled={busy === row.competitor_product_id || scrapingId === row.competitor_product_id}
              onClick={() => runFindMatches(row.competitor_product_id)}
            >
              Find match
            </button>
            <button
              type="button"
              disabled={busy === row.competitor_product_id}
              onClick={() => openReviewChooser(row.competitor_product_id)}
            >
              {isReviewableMatchStatus(row.match_status) ? "Review match" : "Choose product"}
            </button>
          </div>
        );
    }
  }

  function workspaceCellStyle(column: WorkspaceColumnKey): CSSProperties | undefined {
    switch (column) {
      case "image":
        return { width: 64 };
      case "title":
        return { maxWidth: 200 };
      case "category_path":
        return { maxWidth: 220, fontSize: "0.82rem" };
      case "url":
        return { maxWidth: 200 };
      case "brand":
      case "code":
      case "ean":
      case "mfr_model":
      case "size":
      case "color":
        return { fontSize: "0.78rem" };
      case "product_price":
        return { fontWeight: 600 };
      case "offered_by":
      case "delivered_by":
        return { fontSize: "0.78rem", maxWidth: 130 };
      case "matched_name":
        return { maxWidth: 160 };
      case "match":
        return { fontSize: "0.75rem", maxWidth: 120 };
      case "reason":
        return { fontSize: "0.78rem", maxWidth: 200 };
      default:
        return undefined;
    }
  }

  function changeWorkspacePageSize(nextSize: number) {
    setWorkspacePageSize(nextSize);
    setWorkspaceOffset(0);
    void fetchWorkspacePage({ limit: nextSize, offset: 0 });
  }

  function goPrevWorkspacePage() {
    if (!canGoPrev || workspaceLoading) return;
    void fetchWorkspacePage({ offset: Math.max(0, workspaceOffset - workspacePageSize) });
  }

  function goNextWorkspacePage() {
    if (!canGoNext || workspaceLoading) return;
    void fetchWorkspacePage({ offset: workspaceOffset + workspacePageSize });
  }

  function toggleWorkspaceMenu(menu: WorkspacePopoverMenu) {
    setOpenWorkspaceMenu((current) => (current === menu ? null : menu));
  }

  useEffect(() => {
    if (!openWorkspaceMenu) return undefined;

    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest("[data-workspace-popover]")) return;
      setOpenWorkspaceMenu(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenWorkspaceMenu(null);
    };

    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [openWorkspaceMenu]);

  useEffect(() => {
    if (!selectedCategoryId && !selectedCompetitorId) return;
    const handle = window.setTimeout(() => {
      setWorkspaceOffset(0);
      void fetchWorkspacePageRef.current({ offset: 0, search: workspaceSearch });
    }, 350);
    return () => window.clearTimeout(handle);
  }, [selectedCategoryId, selectedCompetitorId, workspaceSearch]);

  useEffect(() => {
    treeAbortRef.current?.abort();
    const ac = new AbortController();
    treeAbortRef.current = ac;
    void refreshTreeRef.current(ac.signal);
    return () => {
      ac.abort();
      if (treeAbortRef.current === ac) {
        treeAbortRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (healthy && tree.length > 0) {
      setBlockingError(null);
    }
  }, [healthy, tree.length]);

  useEffect(() => {
    if (!discoveryAllTaskId) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const status = await api.get<DiscoveryTaskStatus>(
          `/competitors/discovery-tasks/${discoveryAllTaskId}`,
        );
        if (cancelled) return;
        setDiscoveryAllProgress(status);
        if (status.ready) {
          const result = (status.result ?? status) as FullDiscoveryResult;
          if (result.error) {
            setActionError(String(result.error));
          } else {
            setFullDiscoveryDiag(result);
          }

          setDiscoveryAllTaskId(null);
          setDiscoveryAllProgress(null);
          setDiscoveryStopping(false);
          setBusy(null);

          void refreshTreeRef.current().catch(() => null);
          void fetchWorkspacePage({
            offset: workspaceOffset,
            categoryId: selectedCategoryId,
            competitorId: selectedCategoryId ? null : selectedCompetitorId,
          }).catch(() => null);
        }
      } catch (err) {
        if (!cancelled && !isApiAbortError(err)) {
          setActionError(getSectionErrorMessage(err));
          setDiscoveryAllTaskId(null);
          setDiscoveryAllProgress(null);
          setDiscoveryStopping(false);
          setBusy(null);
        }
      }
    };

    void poll();
    const intervalId = window.setInterval(() => void poll(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [
    discoveryAllTaskId,
    selectedCategoryId,
    selectedCompetitorId,
    workspaceOffset,
    fetchWorkspacePage,
  ]);

  // Resume watching a batch scrape that was queued before a page refresh —
  // the Celery task keeps running server-side; only the client forgot it.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(SCRAPE_TASK_STORAGE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw) as { id?: string; ts?: number };
      if (saved.id && Date.now() - (saved.ts ?? 0) < 24 * 3600 * 1000) {
        setScrapeAllTaskId(saved.id);
      } else {
        window.localStorage.removeItem(SCRAPE_TASK_STORAGE_KEY);
      }
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!scrapeAllTaskId) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const status = await api.get<ScrapeTaskStatus>(
          `/competitors/scrape-tasks/${scrapeAllTaskId}`,
        );
        if (cancelled) return;
        setScrapeAllProgress(status);
        if (status.ready) {
          if (status.result?.error) {
            setActionError(String(status.result.error));
          }
          setScrapeStopping(false);
          await fetchWorkspacePage({
            offset: workspaceOffset,
            categoryId: selectedCategoryId,
            competitorId: selectedCategoryId ? null : selectedCompetitorId,
            scrapedFilter: workspaceScrapeFilter,
          });
          await refreshTreeRef.current();
          setScrapeAllTaskId(null);
          setScrapeAllProgress(null);
          setBusy(null);
          try {
            window.localStorage.removeItem(SCRAPE_TASK_STORAGE_KEY);
          } catch {
            /* ignore */
          }
        }
      } catch (err) {
        if (!cancelled && !isApiAbortError(err)) {
          setActionError(getSectionErrorMessage(err));
          setScrapeAllTaskId(null);
          setScrapeAllProgress(null);
          setScrapeStopping(false);
          setBusy(null);
          try {
            window.localStorage.removeItem(SCRAPE_TASK_STORAGE_KEY);
          } catch {
            /* ignore */
          }
        }
      }
    };

    void poll();
    const intervalId = window.setInterval(() => void poll(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [
    scrapeAllTaskId,
    selectedCategoryId,
    selectedCompetitorId,
    workspaceOffset,
    workspaceScrapeFilter,
    fetchWorkspacePage,
  ]);

  useEffect(() => {
    if (!scrapeAllTaskId || scrapeAllProgress?.ready) return;
    if (!selectedCompetitorId) return;

    const refreshTable = () => {
      void fetchWorkspacePage({
        offset: workspaceOffset,
        categoryId: selectedCategoryId,
        competitorId: selectedCategoryId ? null : selectedCompetitorId,
        scrapedFilter: workspaceScrapeFilter,
      });
    };

    refreshTable();
    const intervalId = window.setInterval(refreshTable, 5000);
    return () => window.clearInterval(intervalId);
  }, [
    scrapeAllTaskId,
    scrapeAllProgress?.ready,
    selectedCompetitorId,
    selectedCategoryId,
    workspaceOffset,
    workspaceScrapeFilter,
    fetchWorkspacePage,
  ]);

  useEffect(() => {
    if (!matchAllTaskId) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const status = await api.get<MatchTaskStatus>(`/competitors/match-tasks/${matchAllTaskId}`);
        if (cancelled) return;
        setMatchAllProgress(status);
        if (status.ready) {
          if (status.result?.error) {
            setActionError(String(status.result.error));
          }
          await fetchWorkspacePage({
            offset: workspaceOffset,
            categoryId: selectedCategoryId,
            competitorId: selectedCategoryId ? null : selectedCompetitorId,
            scrapedFilter: workspaceScrapeFilter,
            matchStatus: workspaceMatchFilter,
          });
          setMatchAllTaskId(null);
          setMatchAllProgress(null);
          setBusy(null);
        }
      } catch (err) {
        if (!cancelled && !isApiAbortError(err)) {
          setActionError(getSectionErrorMessage(err));
          setMatchAllTaskId(null);
          setMatchAllProgress(null);
          setBusy(null);
        }
      }
    };

    void poll();
    const intervalId = window.setInterval(() => void poll(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [
    matchAllTaskId,
    selectedCategoryId,
    selectedCompetitorId,
    workspaceOffset,
    workspaceScrapeFilter,
    workspaceMatchFilter,
    fetchWorkspacePage,
  ]);

  useEffect(() => {
    if (!matchAllTaskId || matchAllProgress?.ready) return;
    if (!selectedCompetitorId) return;

    const refreshTable = () => {
      void fetchWorkspacePage({
        offset: workspaceOffset,
        categoryId: selectedCategoryId,
        competitorId: selectedCategoryId ? null : selectedCompetitorId,
        scrapedFilter: workspaceScrapeFilter,
        matchStatus: workspaceMatchFilter,
      });
    };

    refreshTable();
    const intervalId = window.setInterval(refreshTable, 5000);
    return () => window.clearInterval(intervalId);
  }, [
    matchAllTaskId,
    matchAllProgress?.ready,
    selectedCompetitorId,
    selectedCategoryId,
    workspaceOffset,
    workspaceScrapeFilter,
    workspaceMatchFilter,
    fetchWorkspacePage,
  ]);

  const selectedCompetitor = useMemo(
    () => tree.find((c) => c.id === selectedCompetitorId) ?? null,
    [tree, selectedCompetitorId],
  );

  useEffect(() => {
    setConcurrencyDraft(
      selectedCompetitor?.scrape_concurrency_max != null
        ? String(selectedCompetitor.scrape_concurrency_max)
        : "",
    );
  }, [selectedCompetitor?.id, selectedCompetitor?.scrape_concurrency_max]);

  async function saveConcurrencyMax() {
    if (!selectedCompetitor) return;
    const trimmed = concurrencyDraft.trim();
    const parsed = trimmed === "" ? null : Number.parseInt(trimmed, 10);
    if (parsed !== null && (!Number.isFinite(parsed) || parsed < 1 || parsed > 256)) {
      setActionError("Concurrency must be between 1 and 256 (empty = global default).");
      return;
    }
    if (parsed === (selectedCompetitor.scrape_concurrency_max ?? null)) return;
    try {
      await api.put(`/competitors/${selectedCompetitor.id}`, {
        scrape_concurrency_max: parsed,
      });
      await refreshTreeRef.current();
      setActionMessage(
        parsed === null
          ? "Scrape concurrency reset to the global default (applies to the next run)."
          : `Scrape concurrency cap set to ${parsed} for ${selectedCompetitor.name} (applies to the next run).`,
      );
    } catch (err) {
      if (!isApiAbortError(err)) setActionError(getSectionErrorMessage(err));
    }
  }

  const filteredCatalog = useMemo(() => {
    const q = productQuery.trim().toLowerCase();
    if (!q) return catalogProducts.slice(0, 200);
    return catalogProducts.filter(
      (p) =>
        p.sku.toLowerCase().includes(q) ||
        p.name.toLowerCase().includes(q) ||
        (p.ean && p.ean.toLowerCase().includes(q)),
    );
  }, [catalogProducts, productQuery]);

  const catalogBrandOptions = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const product of catalogProducts) {
      const brand = product.brand?.trim();
      if (!brand) continue;
      const key = brand.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(brand);
      if (out.length >= 24) break;
    }
    return out;
  }, [catalogProducts]);

  const patchWorkspaceRow = useCallback(
    (competitorProductId: string, patch: Partial<CategoryWorkspaceProduct>) => {
      setWorkspace((prev) =>
        prev.map((r) =>
          r.competitor_product_id === competitorProductId ? { ...r, ...patch } : r,
        ),
      );
    },
    [],
  );

  async function ensureCatalogLoaded() {
    if (catalogProducts.length > 0) return;
    const page = await api.get<ProductListPage>("/products?limit=100&offset=0");
    setCatalogProducts(page.rows);
  }

  function addDiscoveryTerm(term: string) {
    const clean = term.trim();
    if (!clean) return;
    const current = discoverySeedTerms
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (current.some((x) => x.toLowerCase() === clean.toLowerCase())) return;
    setDiscoverySeedTerms([...current, clean].join(", "));
  }

  async function openReviewChooser(competitorProductId: string) {
    const row = workspace.find((r) => r.competitor_product_id === competitorProductId) ?? null;
    setChooseForId(competitorProductId);
    setChooseRow(row);
    setProductQuery("");
    setChooseError(null);
    setChooseLoading(true);

    let candidates = row?.top_candidates?.length ? [...row.top_candidates] : [];

    try {
      if (candidates.length === 0 && row && isReviewableMatchStatus(row.match_status)) {
        const res = await api.post<FindMatchesResponse>(
          `/competitor-products/${competitorProductId}/find-matches`,
          {},
        );
        candidates = res?.candidates ?? [];
      }
      setChooseCandidates(candidates);
      await ensureCatalogLoaded();
    } catch (err) {
      if (!isApiAbortError(err)) {
        setChooseError(getSectionErrorMessage(err));
        setChooseCandidates([]);
      }
    } finally {
      setChooseLoading(false);
    }
  }

  const closeReviewChooser = useCallback(() => {
    setChooseForId(null);
    setChooseRow(null);
    setChooseCandidates([]);
    setChooseError(null);
    setProductQuery("");
  }, []);

  useEffect(() => {
    if (!chooseForId) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") closeReviewChooser();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [chooseForId, closeReviewChooser]);

  async function addCompetitor(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    try {
      await api.post("/competitors", {
        name: cName,
        domain: cDomain,
        country: cCountry || null,
        currency: cCurrency || "BGN",
        is_active: true,
      });
      setCName("");
      setCDomain("");
      setCCountry("");
      setCCurrency("BGN");
      await refreshTreeAndLists();
    } catch (err) {
      if (!isApiAbortError(err)) {
        setFormError(getSectionErrorMessage(err));
        recordEndpointFailure(err, "/competitors", { allowBlocking: false, hasCachedData: true });
      }
    }
  }

  useEffect(() => {
    if (competitorOptions.length > 0) {
      restoreAddUrlCompetitorSelection(competitorOptions);
    }
  }, [competitorOptions, restoreAddUrlCompetitorSelection]);

  async function addUrl(e: React.FormEvent) {
    e.preventDefault();
    setAddUrlError(null);
    setAddUrlStatus(null);
    if (!addUrlCompetitorId || !singleProductUrl.trim()) {
      setAddUrlError("Choose a competitor and enter a URL.");
      return;
    }
    setBusy("add-url");
    try {
      const res = await api.post<CompetitorProductAddResponse>("/competitor-products", {
        competitor_id: addUrlCompetitorId,
        product_id: null,
        url: singleProductUrl.trim(),
        scrape_after_create: scrapeAfterAdd,
      });
      const created = Boolean(
        res &&
          typeof res === "object" &&
          "created" in res &&
          res.created,
      );
      setAddUrlStatus(
        created
          ? "URL added."
          : "URL already exists & showing existing listing.",
      );
      setSingleProductUrl("");
      setSelectedCompetitorId(addUrlCompetitorId);
      addUrlCompetitorIdRef.current = addUrlCompetitorId;

      let refreshFailed = false;
      try {
        setWorkspaceOffset(0);
        await fetchWorkspacePage({
          offset: 0,
          categoryId: selectedCategoryId,
          competitorId: selectedCategoryId ? null : addUrlCompetitorId,
        });
        await refreshTreeRef.current();
      } catch {
        refreshFailed = true;
      }

      if (refreshFailed) {
        setAddUrlStatus("URL added, but refresh failed. Retry.");
      }

      if (scrapeAfterAdd && isApiJsonBody(res) && res.scrape_task_id) {
        setScrapingId(res.id);
        setTimeout(() => setScrapingId(null), 5000);
      }
    } catch (err) {
      if (!isApiAbortError(err)) {
        setAddUrlError(getSectionErrorMessage(err));
      }
    } finally {
      setBusy(null);
    }
  }

  const workspaceBreadcrumb = useMemo(() => {
    if (!selectedCompetitorId) return "";
    const competitor =
      tree.find((c) => c.id === selectedCompetitorId) ??
      competitorOptions.find((c) => c.id === selectedCompetitorId);
    if (!competitor) return "";
    if (selectedCategoryPath.length === 0) return `${competitor.name} › All products`;
    return [competitor.name, ...selectedCategoryPath].join(" › ");
  }, [tree, competitorOptions, selectedCompetitorId, selectedCategoryPath]);
  const selectedCompetitorName =
    competitorOptions.find((c) => c.id === selectedCompetitorId)?.name ?? "";

  async function discoverAllProductUrls(forceRescan = false) {
    if (!selectedCompetitorId) return;
    setActionError(null);
    setActionMessage(null);
    setFullDiscoveryDiag(null);
    setDiscoveryAllTaskId(null);
    setDiscoveryAllProgress(null);
    setBusy("full-discovery");
    try {
      const queued = readQueuedTaskResponse(
        await api.post<{ task_id: string; message?: string }>(
          `/competitors/${selectedCompetitorId}/discover-all-product-urls`,
          {
            only_new: !forceRescan,
            force_rescan: forceRescan,
            source: quickSiteSearch ? "sitemap" : autoDiscovery ? "auto" : "sitemap",
            discovery_methods: quickSiteSearch
              ? ["site_search"]
              : autoDiscovery
                ? []
                : selectedDiscoveryMethods,
            seed_terms: discoverySeedTerms
              .split(/[\n,]/)
              .map((s) => s.trim())
              .filter(Boolean),
          },
        ),
      );
      if (!queued) {
        setActionError("Discovery queue returned no task id.");
        setBusy(null);
        return;
      }
      setDiscoveryAllTaskId(queued.task_id);
      registerBackgroundTask({
        id: queued.task_id,
        kind: "discovery",
        label: `Find all URLs — ${selectedCompetitor?.name ?? "competitor"}`,
      });
      // Progress lives in the Background activity widget — no toast needed.
    } catch (err) {
      if (!isApiAbortError(err)) {
        setActionError(getSectionErrorMessage(err));
        recordEndpointFailure(err, `/competitors/${selectedCompetitorId}/discover-all-product-urls`, {
          allowBlocking: false,
          hasCachedData: true,
        });
      }
      setDiscoveryAllTaskId(null);
      setDiscoveryAllProgress(null);
      setBusy(null);
    }
  }

  async function scrapeAllProducts() {
    if (!selectedCompetitorId) return;
    setActionError(null);
    setActionMessage(null);
    setScrapeAllTaskId(null);
    setScrapeAllProgress(null);
    setScrapeStopping(false);
    setBusy("scrape-all");
    try {
      const body: {
        category_id?: string;
        only_missing: boolean;
        only_stale: boolean;
      } = {
        only_missing: scrapeOnlyMissing,
        only_stale: false,
      };
      if (selectedCategoryId) {
        body.category_id = selectedCategoryId;
      }
      const queued = readQueuedTaskResponse(
        await api.post<{ task_id: string; message?: string }>(
          `/competitors/${selectedCompetitorId}/scrape-all`,
          body,
        ),
      );
      if (!queued) {
        setActionError("Batch scrape queue returned no task id.");
        setBusy(null);
        return;
      }
      setScrapeAllTaskId(queued.task_id);
      registerBackgroundTask({
        id: queued.task_id,
        kind: "scrape",
        label: `Scrape all — ${selectedCompetitor?.name ?? "competitor"}${scrapeOnlyMissing ? " (only unscraped)" : ""}`,
      });
      setScrapeAllProgress(null);
      try {
        window.localStorage.setItem(
          SCRAPE_TASK_STORAGE_KEY,
          JSON.stringify({ id: queued.task_id, ts: Date.now() }),
        );
      } catch {
        /* storage unavailable — refresh resume just won't work */
      }
      // Progress lives in the Background activity widget — no toast needed.
    } catch (err) {
      if (!isApiAbortError(err)) {
        setActionError(getSectionErrorMessage(err));
        recordEndpointFailure(err, `/competitors/${selectedCompetitorId}/scrape-all`, {
          allowBlocking: false,
          hasCachedData: true,
        });
      }
      setScrapeAllTaskId(null);
      setScrapeAllProgress(null);
      setBusy(null);
    }
  }

  async function stopDiscoveryAll() {
    if (!discoveryAllTaskId || discoveryStopping) return;
    setDiscoveryStopping(true);
    try {
      await api.post(`/competitors/discovery-tasks/${discoveryAllTaskId}/cancel`, {});
      setActionMessage("Stop requested — saving the URLs found so far…");
    } catch (err) {
      setDiscoveryStopping(false);
      if (!isApiAbortError(err)) {
        setActionError(getSectionErrorMessage(err));
      }
    }
  }

  async function stopScrapeAll() {
    if (!scrapeAllTaskId || scrapeStopping) return;
    setScrapeStopping(true);
    try {
      await api.post(`/competitors/scrape-tasks/${scrapeAllTaskId}/cancel`, {});
      setActionMessage("Stop requested — finishing the current chunk…");
    } catch (err) {
      setScrapeStopping(false);
      if (!isApiAbortError(err)) {
        setActionError(getSectionErrorMessage(err));
      }
    }
  }

  async function matchAllProducts() {
    if (!selectedCompetitorId) return;
    setActionError(null);
    setActionMessage(null);
    setMatchAllTaskId(null);
    setMatchAllProgress(null);
    setBusy("match-all");
    try {
      const body: {
        category_id?: string;
        only_unmatched: boolean;
        min_score: number;
      } = {
        // Recompute all suggestions on every run; manually confirmed and
        // rejected matches are always preserved server-side.
        only_unmatched: false,
        min_score: 60,
      };
      if (selectedCategoryId) {
        body.category_id = selectedCategoryId;
      }
      const queued = readQueuedTaskResponse(
        await api.post<{ task_id: string; message?: string }>(
          `/competitors/${selectedCompetitorId}/match-all`,
          body,
        ),
      );
      if (!queued) {
        setActionError("Batch match queue returned no task id.");
        setBusy(null);
        return;
      }
      setMatchAllTaskId(queued.task_id);
      registerBackgroundTask({
        id: queued.task_id,
        kind: "match",
        label: `Match all — ${selectedCompetitor?.name ?? "competitor"}`,
      });
      setMatchAllProgress(null);
      // Progress lives in the Background activity widget — no toast needed.
    } catch (err) {
      if (!isApiAbortError(err)) {
        setActionError(getSectionErrorMessage(err));
        recordEndpointFailure(err, `/competitors/${selectedCompetitorId}/match-all`, {
          allowBlocking: false,
          hasCachedData: true,
        });
      }
      setMatchAllTaskId(null);
      setMatchAllProgress(null);
      setBusy(null);
    }
  }

  async function exportWorkspaceExcel() {
    if (!selectedCompetitorId) return;
    setActionError(null);
    const params = new URLSearchParams({
      sort_by: "last_scraped_at",
      sort_dir: "desc",
    });
    if (selectedCategoryId) {
      params.set("category_id", selectedCategoryId);
    }
    if (workspaceScrapeFilter === "scraped") {
      params.set("scraped", "true");
    } else if (workspaceScrapeFilter === "not_scraped") {
      params.set("scraped", "false");
    }
    if (workspaceMatchFilter !== "all") {
      params.set("status", workspaceMatchFilter);
    }
    if (workspaceSearch.trim()) {
      params.set("search", workspaceSearch.trim());
    }

    const safeName = (workspaceBreadcrumb || "competitors")
      .replace(/[^a-z0-9а-яА-Я]+/gi, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 80);
    try {
      await downloadApiFile(
        `/competitors/${selectedCompetitorId}/products/export-xlsx?${params.toString()}`,
        `${safeName || "competitors"}_export.xlsx`,
      );
    } catch (err) {
      if (!isApiAbortError(err)) {
        setActionError(getSectionErrorMessage(err));
        recordEndpointFailure(err, `/competitors/${selectedCompetitorId}/products/export-xlsx`, {
          allowBlocking: false,
          hasCachedData: workspace.length > 0,
        });
      }
    }
  }

  async function scrapeListing(id: string) {
    const beforeSeen = workspace.find((r) => r.competitor_product_id === id)?.last_seen_at ?? null;
    setScrapingId(id);
    setActionError(null);
    try {
      await api.post(`/jobs/scrape-product/${id}`);
      const deadline = Date.now() + 90_000;
      let scrapedCategoryId: string | null = null;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2000));
        const cp = await api.get<{ last_seen_at: string | null; competitor_category_id: string | null }>(
          `/competitor-products/${id}`,
        );
        if (cp.last_seen_at != null && cp.last_seen_at !== beforeSeen) {
          scrapedCategoryId = cp.competitor_category_id;
          break;
        }
      }
      const latestTree = await api.get<CompetitorTreeItem[]>("/competitors/tree");
      setTree(latestTree);
      const compNode = latestTree.find((c) => c.id === selectedCompetitorId) ?? null;
      if (scrapedCategoryId && compNode) {
        const path = findCategoryPathInTree(compNode.categories, scrapedCategoryId) ?? [];
        pickCategory(scrapedCategoryId, path);
      } else {
        await refreshTreeAndLists();
        await refreshWorkspace();
      }
    } catch (err) {
      if (!isApiAbortError(err)) {
        setActionError(getSectionErrorMessage(err));
        recordEndpointFailure(err, `/jobs/scrape-product/${id}`, {
          allowBlocking: false,
          hasCachedData: true,
        });
      }
    } finally {
      setScrapingId(null);
    }
  }

  async function runFindMatches(id: string) {
    setBusy(id);
    setActionError(null);
    setFindForId(id);
    setCandidates([]);
    try {
      const res = await api.post<FindMatchesResponse>(`/competitor-products/${id}/find-matches`, {});
      setCandidates(res?.candidates ?? []);
    } catch (err) {
      if (!isApiAbortError(err)) {
        setActionError(getSectionErrorMessage(err));
        recordEndpointFailure(err, `/competitor-products/${id}/find-matches`, {
          allowBlocking: false,
          hasCachedData: true,
        });
      }
      setFindForId(null);
    } finally {
      setBusy(null);
    }
  }

  async function confirmMatch(cpId: string, c: MatchCandidate) {
    setBusy(cpId);
    setActionError(null);
    setChooseError(null);
    try {
      await api.post("/matches/confirm", {
        product_id: c.product_id,
        competitor_product_id: cpId,
        match_score: c.match_score,
        match_method: c.match_method,
      });
      setFindForId(null);
      setCandidates([]);
      closeReviewChooser();
      patchWorkspaceRow(cpId, {
        match_status: "confirmed",
        matched_product_id: c.product_id,
        matched_sku: c.sku,
        matched_product_name: c.name,
        match_score: c.match_score,
        match_method: c.match_method,
        match_reason: "Confirmed manually",
        top_candidates: [],
        candidate_count: 0,
      });
      setActionMessage(`Match confirmed: ${c.sku} → listing.`);
      void refreshWorkspace();
      void refreshTreeAndLists();
    } catch (err) {
      if (!isApiAbortError(err)) {
        const msg = getSectionErrorMessage(err);
        setActionError(msg);
        setChooseError(msg);
      }
    } finally {
      setBusy(null);
    }
  }

  async function manualPickProduct(cpId: string, product: Product) {
    setBusy(cpId);
    setActionError(null);
    setChooseError(null);
    try {
      await api.post("/matches/confirm", {
        product_id: product.id,
        competitor_product_id: cpId,
        match_score: "100",
        match_method: "manual_pick",
      });
      closeReviewChooser();
      patchWorkspaceRow(cpId, {
        match_status: "confirmed",
        matched_product_id: product.id,
        matched_sku: product.sku,
        matched_product_name: product.name,
        match_score: "100",
        match_method: "manual_pick",
        match_reason: "Confirmed manually (catalog pick)",
        top_candidates: [],
        candidate_count: 0,
      });
      setActionMessage(`Match confirmed: ${product.sku}.`);
      void refreshWorkspace();
      void refreshTreeAndLists();
    } catch (err) {
      if (!isApiAbortError(err)) {
        const msg = getSectionErrorMessage(err);
        setActionError(msg);
        setChooseError(msg);
      }
    } finally {
      setBusy(null);
    }
  }

  async function rejectMatch(cpId: string, productId: string) {
    setBusy(cpId);
    setActionError(null);
    setChooseError(null);
    try {
      await api.post("/matches/reject", {
        product_id: productId,
        competitor_product_id: cpId,
        reason: null,
      });
      setFindForId(null);
      setCandidates([]);
      if (chooseForId === cpId) {
        closeReviewChooser();
      }
      patchWorkspaceRow(cpId, {
        match_status: "rejected",
        matched_product_id: null,
        matched_sku: null,
        matched_product_name: null,
        match_score: null,
        match_method: null,
        match_reason: "Rejected — no match",
        top_candidates: [],
        candidate_count: 0,
      });
      setActionMessage("Match rejected for this listing.");
      void refreshWorkspace();
    } catch (err) {
      if (!isApiAbortError(err)) {
        const msg = getSectionErrorMessage(err);
        setActionError(msg);
        setChooseError(msg);
      }
    } finally {
      setBusy(null);
    }
  }

  async function rejectListingNoMatch(row: CategoryWorkspaceProduct) {
    const cpId = row.competitor_product_id;
    let productId =
      row.matched_product_id ??
      row.top_candidates?.[0]?.product_id ??
      null;

    if (!productId) {
      setBusy(cpId);
      setChooseError(null);
      try {
        const res = await api.post<FindMatchesResponse>(`/competitor-products/${cpId}/find-matches`, {});
        productId = res?.candidates?.[0]?.product_id ?? null;
      } catch (err) {
        if (!isApiAbortError(err)) {
          setChooseError(getSectionErrorMessage(err));
        }
        setBusy(null);
        return;
      } finally {
        setBusy(null);
      }
    }

    if (!productId) {
      setChooseError("No suggested product to reject. Run Find match or pick a catalog product first.");
      return;
    }

    await rejectMatch(cpId, productId);
  }

  function retryTreeLoad() {
    treeAbortRef.current?.abort();
    const ac = new AbortController();
    treeAbortRef.current = ac;
    void refreshTreeRef.current(ac.signal);
  }

  async function deleteCompetitor(c: { id: string; name: string; domain: string }) {
    const confirmed = window.confirm(
      `Delete retailer "${c.name}" (${c.domain})?\n\n` +
        "This permanently removes ALL its data: product URLs, prices, categories and matches. This cannot be undone.",
    );
    if (!confirmed) return;
    setDeletingCompetitorId(c.id);
    setActionError(null);
    try {
      await api.delete(`/competitors/${c.id}`);
      if (selectedCompetitorId === c.id) {
        setSelectedCompetitorId(null);
        setSelectedCategoryId(null);
        setSelectedCategoryPath([]);
        setWorkspace([]);
      }
      if (addUrlCompetitorIdRef.current === c.id) {
        addUrlCompetitorIdRef.current = "";
        setAddUrlCompetitorId("");
      }
      setActionMessage(`Retailer "${c.name}" and all its data were deleted.`);
      await refreshTreeAndLists();
    } catch (err) {
      setActionError(getSectionErrorMessage(err));
      recordEndpointFailure(err, `/competitors/${c.id}`, {});
    } finally {
      setDeletingCompetitorId(null);
    }
  }

  const sidebarWorkspaceControls = selectedCompetitorId ? (
    <div className="sidebar-workspace-controls">
      <div className="sidebar-control-heading">
        <span>Workspace</span>
        <small>{workspaceBreadcrumb || "Select retailer"}</small>
      </div>
      <div className="sidebar-action-grid">
        <button
          type="button"
          className="primary"
          disabled={
            discoveryAllRunning ||
            busy === "batch" ||
            busy === "discover-products" ||
            matchAllRunning ||
            scrapeAllRunning ||
            siteSearchOnlyNeedsTerm
          }
          onClick={() => void discoverAllProductUrls(false)}
        >
          {discoveryAllRunning ? "Finding…" : "Find URLs"}
        </button>
      </div>
      <details className="sidebar-discovery-settings">
        <summary>Discovery settings</summary>
        <div className="sidebar-discovery-options">
          <label className="inline-checkbox">
            <input
              type="checkbox"
              checked={autoDiscovery}
              onChange={(e) => setAutoDiscovery(e.target.checked)}
              disabled={discoveryAllRunning}
            />
            Auto probe
          </label>
          <label className="inline-checkbox">
            <input
              type="checkbox"
              checked={quickSiteSearch}
              onChange={(e) => {
                setQuickSiteSearch(e.target.checked);
                if (e.target.checked) {
                  void ensureCatalogLoaded();
                }
              }}
              disabled={discoveryAllRunning}
            />
            Site search
          </label>
          {!autoDiscovery && !quickSiteSearch
            ? DISCOVERY_METHOD_OPTIONS.map((method) => (
                <label className="inline-checkbox" key={method.id}>
                  <input
                    type="checkbox"
                    checked={selectedDiscoveryMethods.includes(method.id)}
                    onChange={(e) => {
                      if (e.target.checked && method.id === "site_search") {
                        void ensureCatalogLoaded();
                      }
                      setSelectedDiscoveryMethods((current) => {
                        if (e.target.checked) {
                          return current.includes(method.id) ? current : [...current, method.id].slice(0, 5);
                        }
                        const next = current.filter((x) => x !== method.id);
                        return next.length ? next : ["sitemap"];
                      });
                    }}
                    disabled={discoveryAllRunning}
                  />
                  {method.label}
                </label>
              ))
            : null}
          <input
            value={discoverySeedTerms}
            onChange={(e) => setDiscoverySeedTerms(e.target.value)}
            disabled={
              discoveryAllRunning ||
              (!quickSiteSearch &&
                !autoDiscovery &&
                !selectedDiscoveryMethods.includes("external_search") &&
                !selectedDiscoveryMethods.includes("site_search"))
            }
            placeholder="Search terms / brands"
          />
          {(quickSiteSearch || selectedDiscoveryMethods.includes("site_search")) && catalogBrandOptions.length > 0 ? (
            <div className="discovery-brand-picks sidebar-brand-picks">
              {catalogBrandOptions.slice(0, 6).map((brand) => (
                <button key={brand} type="button" disabled={discoveryAllRunning} onClick={() => addDiscoveryTerm(brand)}>
                  {brand}
                </button>
              ))}
            </div>
          ) : null}
          {siteSearchOnlyNeedsTerm ? (
            <span className="muted discovery-method-hint">Site search needs a term.</span>
          ) : null}
        </div>
      </details>
    </div>
  ) : null;

  return (
    <div className="competitors-page">
      {blockingError ? (
        <div className="blocking-error-banner" role="alert">
          <p>{blockingError}</p>
          <p className="muted" style={{ marginBottom: 0 }}>
            Check the API status indicator in the top bar, then retry.
          </p>
        </div>
      ) : null}
      {actionMessage ? <div className="toast">{actionMessage}</div> : null}
      {actionError ? <div className="toast toast-error">{actionError}</div> : null}

      <div className="competitors-split">
        <section className="competitors-left card">
          <div className="panel-heading">
            <div>
              <p className="panel-kicker">Retailer sources</p>
              <h2>Catalog Explorer</h2>
            </div>
            <span className="panel-count">{tree.length}</span>
          </div>
          <div className="panel-accent" />

          <details className="catalog-add-details">
            <summary>+ Add retailer</summary>
            <form onSubmit={addCompetitor} className="row catalog-form">
              <div className="field">
                <label>Name</label>
                <input value={cName} onChange={(e) => setCName(e.target.value)} required />
              </div>
              <div className="field">
                <label>Domain</label>
                <input
                  value={cDomain}
                  onChange={(e) => setCDomain(e.target.value)}
                  required
                />
              </div>
              <button className="primary" type="submit">
                Add retailer
              </button>
            </form>
          </details>
          {formError ? <p className="section-inline-error">{formError}</p> : null}
          {sidebarWorkspaceControls}

          {treeError ? (
            <div className="section-inline-error tree-section-error">
              <span>{treeError}</span>
              <button type="button" onClick={retryTreeLoad} disabled={treeRefreshing}>
                {treeRefreshing ? "Retrying…" : "Retry"}
              </button>
            </div>
          ) : null}
          {treeStale ? <p className="muted tree-stale-hint">Showing last loaded catalog tree.</p> : null}

          <div className="retailer-list-heading">
            <span>Your retailers</span>
            <span>{retailerSearch ? `${groupedRetailers.reduce((n, g) => n + g.retailers.length, 0)} / ${tree.length}` : tree.length}</span>
          </div>
          <div className="retailer-search">
            <input
              value={retailerSearch}
              onChange={(e) => setRetailerSearch(e.target.value)}
              placeholder="Search retailer or domain..."
              aria-label="Search retailers"
            />
            {retailerSearch ? (
              <button type="button" onClick={() => setRetailerSearch("")}>
                Clear
              </button>
            ) : null}
          </div>
          <div className="explorer-list">
            {groupedRetailers.length === 0 ? (
              <p className="muted retailer-empty">No retailers match your search.</p>
            ) : null}
            {groupedRetailers.map((group) => (
              <details
                key={group.country}
                className="retailer-country-group"
                open={retailerSearch.length > 0 || group.retailers.some((c) => c.id === selectedCompetitorId)}
              >
                <summary>
                  <span>{group.label}</span>
                  <span>{group.retailers.length}</span>
                </summary>
                <div className="retailer-country-list">
                {group.retailers.map((c) => (
                  <div key={c.id} className="explorer-competitor">
                <div className="explorer-head-row">
                  <button
                    type="button"
                    className={selectedCompetitorId === c.id ? "explorer-head explorer-head-active" : "explorer-head"}
                    onClick={() => {
                      setSelectedCompetitorId(c.id);
                      setSelectedCategoryId(null);
                      setSelectedCategoryPath([]);
                      setWorkspaceOffset(0);
                      // Reports belong to the previously selected retailer — hide
                      // them on switch (unless a task is still running).
                      setFullDiscoveryDiag(null);
                      if (!scrapeAllTaskId) setScrapeAllProgress(null);
                      if (!matchAllTaskId) setMatchAllProgress(null);
                      setActionMessage(null);
                      setActionError(null);
                      if (!addUrlCompetitorIdRef.current) {
                        setAddUrlCompetitorId(c.id);
                        addUrlCompetitorIdRef.current = c.id;
                      }
                      void fetchWorkspacePage({ offset: 0, categoryId: null, competitorId: c.id });
                    }}
                  >
                    <RetailerAvatar name={c.name} domain={c.domain} />
                    <span className="retailer-copy">
                      <span className="retailer-name">{c.name}</span>
                      <span className="retailer-domain">{c.domain}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    className="explorer-delete"
                    title={`Delete ${c.name} and all its data`}
                    aria-label={`Delete ${c.name}`}
                    disabled={deletingCompetitorId !== null}
                    onClick={() => void deleteCompetitor(c)}
                  >
                    {deletingCompetitorId === c.id ? "…" : "✕"}
                  </button>
                </div>
                {selectedCompetitorId === c.id ? (
                  <TreeNodes
                    nodes={c.categories}
                    selectedId={selectedCategoryId}
                    onPick={pickCategory}
                    depth={0}
                  />
                ) : null}
              </div>
                ))}
                </div>
              </details>
            ))}
          </div>
        </section>

        <section className="competitors-workspace">

            {discoveryAllProgress && (discoveryAllRunning || discoveryAllProgress.ready) ? (
              <DiscoveryProgressPanel
                progress={discoveryAllProgress}
                running={discoveryAllRunning}
                onStop={() => void stopDiscoveryAll()}
                stopping={discoveryStopping}
              />
            ) : null}

            {scrapeAllProgress && (scrapeAllRunning || scrapeAllProgress.ready) ? (
              <ScrapeProgressPanel
                progress={scrapeAllProgress}
                running={scrapeAllRunning}
                onStop={() => void stopScrapeAll()}
                stopping={scrapeStopping}
              />
            ) : null}

            {matchAllProgress && (matchAllRunning || matchAllProgress.ready) ? (
              <MatchProgressPanel progress={matchAllProgress} running={matchAllRunning} />
            ) : null}

          {fullDiscoveryDiag ? (
            <div className="card full-discovery-diag" style={{ marginBottom: "1rem" }}>
              <h3 style={{ marginTop: 0, fontSize: "1rem" }}>Full discovery results</h3>
              {fullDiscoveryDiag.public_discovery_blocked ? (
                <p className="section-inline-error">
                  {fullDiscoveryDiag.discovery_block_reason === "site_unreachable"
                    ? "The site drops every connection from this server (IP-level block or dead host) — discovery was aborted early. A proxy for this retailer is likely required."
                    : `Public discovery is blocked by the target site${
                        fullDiscoveryDiag.discovery_block_reason
                          ? ` (${fullDiscoveryDiag.discovery_block_reason})`
                          : ""
                      }. Try a direct product URL or an allowed feed/export for this retailer.`}
                </p>
              ) : null}
              <ul className="full-discovery-stats">
                <li>Source: {fullDiscoveryDiag.source ?? "—"}</li>
                <li>Product URLs found: {fullDiscoveryDiag.product_urls_found ?? 0}</li>
                {fullDiscoveryDiag.limit_reached ? (
                  <li>
                    Limit reached: {fullDiscoveryDiag.max_products?.toLocaleString() ?? "yes"}
                  </li>
                ) : null}
                <li>New URLs: {fullDiscoveryDiag.new_urls_found ?? 0}</li>
                <li>Created: {fullDiscoveryDiag.created ?? 0}</li>
                <li>Skipped (existing): {fullDiscoveryDiag.skipped_existing ?? 0}</li>
                <li>Categories updated: {fullDiscoveryDiag.categories_updated ?? 0}</li>
                <li>Pages scanned: {fullDiscoveryDiag.pages_scanned ?? 0}</li>
                <li>External queries: {fullDiscoveryDiag.external_queries_checked ?? 0}</li>
                {(fullDiscoveryDiag.rate_limit_pauses ?? 0) > 0 ? (
                  <li>Rate-limit waits: {fullDiscoveryDiag.rate_limit_pauses}</li>
                ) : null}
                {fullDiscoveryDiag.deep_discovery ? (
                  <li>Seed terms: {fullDiscoveryDiag.seed_terms_used ?? 0}</li>
                ) : null}
                <li>
                  Sitemaps checked:{" "}
                  {fullDiscoveryDiag.sitemap_files_checked ??
                    fullDiscoveryDiag.sitemap_urls_checked ??
                    0}
                </li>
                {fullDiscoveryDiag.duration_ms != null ? (
                  <li>Duration: {Math.round(fullDiscoveryDiag.duration_ms / 1000)}s</li>
                ) : null}
              </ul>
              {fullDiscoveryDiag.sample_product_urls?.length ? (
                <details>
                  <summary>Sample product URLs</summary>
                  <ul style={{ fontSize: "0.82rem", wordBreak: "break-all" }}>
                    {fullDiscoveryDiag.sample_product_urls.map((u) => (
                      <li key={u}>{u}</li>
                    ))}
                  </ul>
                </details>
              ) : null}
              {fullDiscoveryDiag.errors?.length ? (
                <details>
                  <summary>Errors ({fullDiscoveryDiag.errors.length})</summary>
                  <ul style={{ fontSize: "0.82rem" }}>
                    {fullDiscoveryDiag.errors.slice(0, 20).map((e, i) => (
                      <li key={`${i}-${e}`}>{e}</li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </div>
          ) : null}

          {selectedCompetitorId ? (
            <CompetitorStatsDashboard
              competitorId={selectedCompetitorId}
              competitorName={selectedCompetitorName}
              refreshKey={workspaceTotal}
            />
          ) : null}

          {!selectedCompetitorId ? (
            <div className="card workspace-empty">
              <div className="empty-illustration" aria-hidden>
                <span className="empty-card" />
                <span className="empty-lens" />
                <span className="empty-sparkle empty-sparkle-one" />
                <span className="empty-sparkle empty-sparkle-two" />
              </div>
              <h3>Select a retailer</h3>
              <p>Choose a retailer from the Catalog Explorer to view discovered products, prices, and match status.</p>
            </div>
          ) : workspaceLoading && workspace.length === 0 ? (
            <div className="card workspace-results-card workspace-loading">
              <div className="workspace-results-header">
                <h3>{selectedCompetitorName || "Loading"} products</h3>
                <span className="muted">Loading catalog data…</span>
              </div>
            <div className="table-scroll workspace-table-wrap">
              <table className="compact-table workspace-table">
                <thead>
                  <tr>
                    <th>Image</th>
                    <th>Title</th>
                    <th>Category path</th>
                    <th>URL</th>
                    <th>Code</th>
                    <th>Size</th>
                    <th>Final EUR</th>
                    <th>Regular EUR</th>
                    <th>Promo EUR</th>
                    <th>Old/list EUR</th>
                    <th>Currency</th>
                    <th>Availability</th>
                    <th>Last checked</th>
                    <th>Matched SKU</th>
                    <th>Matched name</th>
                    <th>Score</th>
                    <th>Match</th>
                    <th>Status</th>
                    <th>Reason</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {Array.from({ length: 8 }).map((_, i) => (
                    <tr key={`skel-${i}`} className="workspace-skeleton-row">
                      {Array.from({ length: 25 }).map((__, j) => (
                        <td key={j}>
                          <span className="workspace-skeleton-cell" />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            </div>
          ) : workspaceError ? (
            <div className="section-inline-error">
              <span>{workspaceError}</span>
              <button type="button" onClick={() => void refreshWorkspace()}>
                Retry
              </button>
            </div>
          ) : workspaceTotal === 0 && !hasWorkspaceFilters ? (
            <div className="card workspace-empty">
              <div className="empty-illustration" aria-hidden>
                <span className="empty-card" />
                <span className="empty-lens" />
                <span className="empty-sparkle empty-sparkle-one" />
                <span className="empty-sparkle empty-sparkle-two" />
              </div>
              <h3>No products discovered yet</h3>
              <p>
                {selectedCategoryId
                  ? "Run discovery to find product URLs for this category and start tracking prices."
                  : "Run discovery to find product URLs and start tracking prices for this retailer."}
              </p>
              <div className="empty-actions">
                <button
                  type="button"
                  className="primary"
                  disabled={discoveryAllRunning || busy === "full-discovery" || siteSearchOnlyNeedsTerm}
                  onClick={() => void discoverAllProductUrls(false)}
                >
                  {discoveryAllRunning ? "Finding all URLs…" : "Find all URLs"}
                </button>
                <details className="discovery-help">
                  <summary>How discovery works</summary>
                  <p>
                    Ozypricing probes the retailer site, chooses discovery methods, stores product URLs,
                    and then enables scraping and matching from this workspace.
                  </p>
                </details>
              </div>
            </div>
          ) : (
            <div className="card workspace-results-card">
              <div className="workspace-results-header">
                <div>
                  <p className="panel-kicker">Tracked catalog</p>
                  <h3>{selectedCompetitorName || "Retailer"} products</h3>
                </div>
                <div className="workspace-header-actions">
                  <span className="badge badge-info">{workspaceTotal.toLocaleString()} URLs</span>
                  <button type="button" disabled={workspaceLoading} onClick={() => void exportWorkspaceExcel()}>
                    Export
                  </button>
                  <div className="workspace-popover workspace-action-menu" data-workspace-popover>
                    <button
                      type="button"
                      className={openWorkspaceMenu === "scrape" ? "workspace-popover-trigger-active" : undefined}
                      aria-expanded={openWorkspaceMenu === "scrape"}
                      onClick={() => toggleWorkspaceMenu("scrape")}
                    >
                      {scrapeAllRunning || busy === "scrape-all" ? "Scraping…" : "Scrape"}
                    </button>
                    {openWorkspaceMenu === "scrape" ? (
                      <div className="workspace-popover-panel workspace-action-menu-panel">
                        <label className="inline-checkbox">
                          <input
                            type="checkbox"
                            checked={scrapeOnlyMissing}
                            onChange={(e) => setScrapeOnlyMissing(e.target.checked)}
                            disabled={scrapeAllRunning || busy === "scrape-all"}
                          />
                          Only unscraped URLs
                        </label>
                        <button
                          type="button"
                          className="primary"
                          disabled={
                            scrapeAllRunning ||
                            discoveryAllRunning ||
                            busy === "batch" ||
                            busy === "discover-products" ||
                            busy === "full-discovery" ||
                            busy === "scrape-all"
                          }
                          onClick={() => void scrapeAllProducts()}
                        >
                          {scrapeAllRunning || busy === "scrape-all" ? "Scraping…" : "Run scrape"}
                        </button>
                      </div>
                    ) : null}
                  </div>
                  <div className="workspace-popover workspace-action-menu" data-workspace-popover>
                    <button
                      type="button"
                      className={openWorkspaceMenu === "match" ? "workspace-popover-trigger-active" : undefined}
                      aria-expanded={openWorkspaceMenu === "match"}
                      onClick={() => toggleWorkspaceMenu("match")}
                    >
                      {matchAllRunning || busy === "match-all" ? "Matching…" : "Match"}
                    </button>
                    {openWorkspaceMenu === "match" ? (
                      <div className="workspace-popover-panel workspace-action-menu-panel">
                        <span className="muted">Find best catalog matches for the current retailer.</span>
                        <button
                          type="button"
                          className="primary"
                          disabled={matchAllRunning || scrapeAllRunning || discoveryAllRunning || busy === "match-all"}
                          onClick={() => void matchAllProducts()}
                        >
                          {matchAllRunning || busy === "match-all" ? "Matching…" : "Run match"}
                        </button>
                      </div>
                    ) : null}
                  </div>
                </div>
              </div>
              <div className="workspace-table-filters workspace-table-filters-wrap">
                <input
                  className="workspace-search-input"
                  value={workspaceSearch}
                  onChange={(e) => setWorkspaceSearch(e.target.value)}
                  placeholder="Search product, barcode, brand, code..."
                  disabled={workspaceLoading}
                />
                {workspaceSearch ? (
                  <button
                    type="button"
                    disabled={workspaceLoading}
                    onClick={() => setWorkspaceSearch("")}
                  >
                    Clear
                  </button>
                ) : null}
                <label className="workspace-filter-select">
                  <span className="muted">Scrape:</span>
                  <select
                    value={workspaceScrapeFilter}
                    disabled={workspaceLoading}
                    onChange={(e) => {
                      const filter = e.target.value as WorkspaceScrapeFilter;
                      setWorkspaceScrapeFilter(filter);
                      setWorkspaceOffset(0);
                      void fetchWorkspacePage({
                        offset: 0,
                        limit: workspacePageSize,
                        scrapedFilter: filter,
                      });
                    }}
                  >
                    {WORKSPACE_SCRAPE_FILTER_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="workspace-filter-select">
                  <span className="muted">Match:</span>
                  <select
                    value={workspaceMatchFilter}
                    disabled={workspaceLoading}
                    onChange={(e) => {
                      const filter = e.target.value as WorkspaceMatchFilter;
                      setWorkspaceMatchFilter(filter);
                      setWorkspaceOffset(0);
                      void fetchWorkspacePage({
                        offset: 0,
                        limit: workspacePageSize,
                        matchStatus: filter,
                      });
                    }}
                  >
                    {WORKSPACE_MATCH_FILTER_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="workspace-popover workspace-pin-menu" data-workspace-popover>
                  <button
                    type="button"
                    className={openWorkspaceMenu === "pin" ? "workspace-popover-trigger-active" : undefined}
                    aria-expanded={openWorkspaceMenu === "pin"}
                    onClick={() => toggleWorkspaceMenu("pin")}
                  >
                    Pin columns
                  </button>
                  {openWorkspaceMenu === "pin" ? (
                    <div className="workspace-popover-panel workspace-column-menu-panel">
                      <div className="workspace-pin-options">
                        {WORKSPACE_COLUMNS.map((column) => (
                          <label key={column.key} className="workspace-pin-option">
                            <input
                              type="checkbox"
                              checked={workspacePinnedColumnSet.has(column.key)}
                              onChange={() => toggleWorkspacePinnedColumn(column.key)}
                            />
                            <span>{column.label}</span>
                          </label>
                        ))}
                      </div>
                      {workspacePinnedColumns.length > 0 ? (
                        <button type="button" onClick={() => setWorkspacePinnedColumns([])}>
                          Clear pinned
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                </div>
                <div className="workspace-popover workspace-pin-menu" data-workspace-popover>
                  <button
                    type="button"
                    className={openWorkspaceMenu === "hide" ? "workspace-popover-trigger-active" : undefined}
                    aria-expanded={openWorkspaceMenu === "hide"}
                    onClick={() => toggleWorkspaceMenu("hide")}
                  >
                    Hide columns
                  </button>
                  {openWorkspaceMenu === "hide" ? (
                    <div className="workspace-popover-panel workspace-column-menu-panel">
                      <div className="workspace-pin-options">
                        {WORKSPACE_COLUMNS.map((column) => (
                          <label key={column.key} className="workspace-pin-option">
                            <input
                              type="checkbox"
                              checked={workspaceHiddenColumnSet.has(column.key)}
                              disabled={visibleWorkspaceColumns.length === 1 && !workspaceHiddenColumnSet.has(column.key)}
                              onChange={() => toggleWorkspaceHiddenColumn(column.key)}
                            />
                            <span>{column.label}</span>
                          </label>
                        ))}
                      </div>
                      {workspaceHiddenColumns.length > 0 ? (
                        <button type="button" onClick={() => setWorkspaceHiddenColumns([])}>
                          Show all columns
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
              <div className={`table-scroll workspace-table-wrap${workspaceLoading ? " workspace-table-busy" : ""}`}>
                <table className="compact-table workspace-table">
                  <thead>
                    <tr>
                      {visibleWorkspaceColumns.map((column) => (
                        <th
                          key={column.key}
                          className={workspacePinClass(column.key)}
                          style={workspacePinStyle(column.key, undefined, "header")}
                        >
                          {column.label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {workspace.length === 0 ? (
                      <tr>
                        <td
                          colSpan={visibleWorkspaceColumns.length}
                          className="muted"
                          style={{ textAlign: "center", padding: "1.25rem" }}
                        >
                          {workspaceEmptyFilterMessage}{" "}
                          {hasWorkspaceFilters ? (
                            <button
                              type="button"
                              disabled={workspaceLoading}
                              onClick={() => {
                                setWorkspaceScrapeFilter("all");
                                setWorkspaceMatchFilter("all");
                                setWorkspaceSearch("");
                                setWorkspaceOffset(0);
                                void fetchWorkspacePage({
                                  offset: 0,
                                  limit: workspacePageSize,
                                  scrapedFilter: "all",
                                  matchStatus: "all",
                                  search: "",
                                });
                              }}
                            >
                              Show all
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    ) : workspace.map((row) => (
                      <tr key={row.competitor_product_id}>
                        {visibleWorkspaceColumns.map((column) => (
                          <td
                            key={column.key}
                            className={workspacePinClass(column.key)}
                            style={workspacePinStyle(column.key, workspaceCellStyle(column.key))}
                            title={
                              column.key === "offered_by"
                                ? row.offered_by ?? undefined
                                : column.key === "delivered_by"
                                  ? row.delivered_by ?? undefined
                                  : undefined
                            }
                          >
                            {renderWorkspaceCell(row, column.key)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                  </table>
                </div>
              <div className="workspace-pagination">
                <span className="muted workspace-pagination-range">
                  Showing {workspaceRangeStart}–{workspaceRangeEnd} of {workspaceTotal}
                </span>
                <label className="workspace-page-size">
                  Rows per page{" "}
                  <select
                    value={workspacePageSize}
                    disabled={workspaceLoading}
                    onChange={(e) => changeWorkspacePageSize(Number(e.target.value))}
                  >
                    {WORKSPACE_PAGE_SIZES.map((size) => (
                      <option key={size} value={size}>
                        {size}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="workspace-pagination-actions">
                  <button type="button" disabled={!canGoPrev || workspaceLoading} onClick={goPrevWorkspacePage}>
                    Previous
                  </button>
                  <button type="button" disabled={!canGoNext || workspaceLoading} onClick={goNextWorkspacePage}>
                    Next
                  </button>
                </div>
              </div>
            </div>
          )}

          {findForId && candidates.length > 0 ? (
            <div className="card" style={{ marginTop: "1rem" }}>
              <h3 style={{ marginTop: 0 }}>Match candidates (top 5)</h3>
              <p className="muted">Listing ID: {findForId}</p>
              <table className="compact-table">
                <thead>
                  <tr>
                    <th>SKU</th>
                    <th>Name</th>
                    <th>Brand</th>
                    <th>EAN</th>
                    <th>Mfr code</th>
                    <th>Model</th>
                    <th>Price</th>
                    <th>Score</th>
                    <th>Method</th>
                    <th>Reasons</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((c) => (
                    <tr key={c.product_id}>
                      <td>{c.sku}</td>
                      <td>{c.name}</td>
                      <td>{c.brand ?? "—"}</td>
                      <td>{c.ean ?? "—"}</td>
                      <td>{c.manufacturer_code ?? "—"}</td>
                      <td>{c.model ?? "—"}</td>
                      <td>{fmtMoney(c.own_price)}</td>
                      <td>{c.match_score}</td>
                      <td>{c.match_method}</td>
                      <td style={{ fontSize: "0.78rem", maxWidth: 220 }}>
                        {c.match_reasons?.length ? (
                          <ul className="match-hints-list">
                            {c.match_reasons.map((r) => (
                              <li key={r}>{r}</li>
                            ))}
                          </ul>
                        ) : (
                          "—"
                        )}
                        {c.match_warnings?.length ? (
                          <ul className="match-hints-list match-warnings">
                            {c.match_warnings.map((w) => (
                              <li key={w}>{w}</li>
                            ))}
                          </ul>
                        ) : null}
                      </td>
                      <td>
                        <button type="button" className="primary" onClick={() => confirmMatch(findForId, c)}>
                          Confirm
                        </button>{" "}
                        <button type="button" onClick={() => rejectMatch(findForId, c.product_id)}>
                          Reject
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <button
                type="button"
                style={{ marginTop: "0.5rem" }}
                onClick={() => {
                  setFindForId(null);
                  setCandidates([]);
                }}
              >
                Close
              </button>
            </div>
          ) : null}

          {chooseForId ? (
            <div className="modal-backdrop" role="presentation" onMouseDown={closeReviewChooser}>
            <div
              className="card modal-card review-match-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="review-match-title"
              onMouseDown={(e) => e.stopPropagation()}
            >
              <h3 id="review-match-title" style={{ marginTop: 0 }}>
                {chooseRow && isReviewableMatchStatus(chooseRow.match_status)
                  ? "Manual match review"
                  : "Choose catalog product"}
              </h3>
              <p className="muted">
                Listing: {chooseRow?.title ?? chooseForId}
                {chooseRow?.match_status ? (
                  <>
                    {" "}
                    · Status: <strong>{matchStatusLabel(chooseRow.match_status)}</strong>
                  </>
                ) : null}
              </p>
              {chooseRow ? (
                <div className="match-listing-summary">
                  <ProductImagePreview
                    src={chooseRow.image_url}
                    alt={chooseRow.title ? `Competitor image for ${chooseRow.title}` : "Competitor product image"}
                    label="Competitor"
                  />
                  <div className="match-listing-copy">
                    <strong>{chooseRow.title ?? "Competitor listing"}</strong>
                    <div className="match-identifier-grid">
                      <span>
                        <b>Barcode:</b> {chooseRow.listing_ean || "—"}
                      </span>
                      <span>
                        <b>MFR:</b> {chooseRow.listing_manufacturer_code || "—"}
                      </span>
                      <span>
                        <b>SKU:</b> {chooseRow.listing_sku || "—"}
                      </span>
                      <span>
                        <b>Brand:</b> {chooseRow.listing_brand || "—"}
                      </span>
                    </div>
                  </div>
                </div>
              ) : null}
              {chooseRow?.match_reason ? (
                <p className="muted" style={{ marginTop: 0 }}>
                  {chooseRow.match_reason}
                  {(chooseRow.candidate_count ?? 0) > 0
                    ? ` (${chooseRow.candidate_count ?? 0} candidate${chooseRow.candidate_count === 1 ? "" : "s"} scored)`
                    : null}
                </p>
              ) : null}
              {chooseLoading ? <p className="muted">Loading suggested candidates…</p> : null}
              {chooseError ? <p className="section-inline-error">{chooseError}</p> : null}

              {!chooseLoading && chooseCandidates.length > 0 ? (
                <>
                  <h4 style={{ marginTop: 0, fontSize: "0.95rem" }}>
                    Suggested candidates ({chooseCandidates.length})
                  </h4>
                  <div className="table-scroll" style={{ maxHeight: 260, marginBottom: "0.75rem" }}>
                    <table className="compact-table">
                      <thead>
                        <tr>
                          <th>Image</th>
                          <th>Product</th>
                          <th>SKU</th>
                          <th>EAN</th>
                          <th>Mfr code</th>
                          <th>Brand</th>
                          <th>Score</th>
                          <th>Method</th>
                          <th>Reasons</th>
                          <th />
                        </tr>
                      </thead>
                      <tbody>
                        {chooseCandidates.map((c) => (
                          <tr key={c.product_id}>
                            <td>
                              <ProductImagePreview
                                src={c.image_url}
                                alt={`Catalog image for ${c.name}`}
                                label="Own"
                              />
                            </td>
                            <td>{c.name}</td>
                            <td>{c.sku}</td>
                            <td>{c.ean ?? "—"}</td>
                            <td>{c.manufacturer_code ?? "—"}</td>
                            <td>{c.brand ?? "—"}</td>
                            <td>{c.match_score}</td>
                            <td>{c.match_method}</td>
                            <td style={{ fontSize: "0.78rem", maxWidth: 200 }}>
                              {c.match_reasons?.length ? (
                                <ul className="match-hints-list">
                                  {c.match_reasons.map((r) => (
                                    <li key={r}>{r}</li>
                                  ))}
                                </ul>
                              ) : (
                                "—"
                              )}
                              {c.match_warnings?.length ? (
                                <ul className="match-hints-list match-warnings">
                                  {c.match_warnings.map((w) => (
                                    <li key={w}>{w}</li>
                                  ))}
                                </ul>
                              ) : null}
                            </td>
                            <td style={{ whiteSpace: "nowrap" }}>
                              <button
                                type="button"
                                className="primary"
                                disabled={busy === chooseForId}
                                onClick={() => confirmMatch(chooseForId, c)}
                              >
                                {busy === chooseForId ? "Saving…" : "Confirm match"}
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : null}

              {!chooseLoading && chooseCandidates.length === 0 && chooseRow && isReviewableMatchStatus(chooseRow.match_status) ? (
                <p className="muted">
                  No stored candidates for this listing. Use <strong>Find match</strong> or search the catalog below.
                </p>
              ) : null}

              <details className="review-catalog-details" style={{ marginBottom: "0.75rem" }}>
                <summary>Search full catalog (manual pick)</summary>
                <input
                  placeholder="Search SKU, name, EAN…"
                  value={productQuery}
                  onChange={(e) => setProductQuery(e.target.value)}
                  style={{ width: "100%", margin: "0.5rem 0" }}
                />
                <div className="table-scroll" style={{ maxHeight: 220 }}>
                  <table className="compact-table">
                    <thead>
                      <tr>
                        <th>Image</th>
                        <th>SKU</th>
                        <th>Name</th>
                        <th>Brand</th>
                        <th>EAN</th>
                        <th>Mfr code</th>
                        <th>Own price</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {filteredCatalog.slice(0, 80).map((p) => (
                        <tr key={p.id}>
                          <td>
                            <ProductImagePreview
                              src={p.image_url}
                              alt={`Catalog image for ${p.name}`}
                              label="Own"
                            />
                          </td>
                          <td>{p.sku}</td>
                          <td>{p.name}</td>
                          <td>{p.brand ?? "—"}</td>
                          <td>{p.ean ?? "—"}</td>
                          <td>{p.manufacturer_code ?? "—"}</td>
                          <td>{fmtMoney(p.own_price)}</td>
                          <td>
                            <button
                              type="button"
                              className="primary"
                              disabled={busy === chooseForId}
                              onClick={() => manualPickProduct(chooseForId, p)}
                            >
                              Confirm match
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>

              <div className="review-match-actions">
                {chooseRow ? (
                  <button
                    type="button"
                    disabled={busy === chooseForId || chooseLoading}
                    onClick={() => rejectListingNoMatch(chooseRow)}
                  >
                    {busy === chooseForId ? "Saving…" : "Reject / No match"}
                  </button>
                ) : null}
                <button type="button" onClick={closeReviewChooser}>
                  Close
                </button>
              </div>
            </div>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
