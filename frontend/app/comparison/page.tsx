"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isApiAbortError } from "@/lib/api";
import type {
  ComparisonCompetitor,
  CompetitorPriceLine,
  PriceComparisonPageData,
  PriceComparisonRow,
  PriceComparisonSummary,
  UUID,
} from "@/lib/types";

const PAGE_SIZE = 75;
const LAYOUT_STORAGE_KEY = "pm_comparison_layout_v1";
const MIN_COL_WIDTH = 96;

type ColumnLayout = {
  hidden: string[];
  pinned: string[];
  widths: Record<string, number>;
};

const DEFAULT_LAYOUT: ColumnLayout = {
  hidden: [],
  pinned: ["sku", "name"],
  widths: {},
};

const BASE_COLUMNS: { id: string; label: string; numeric?: boolean; defaultWidth: number }[] = [
  { id: "sku", label: "SKU", defaultWidth: 140 },
  { id: "name", label: "Name", defaultWidth: 360 },
  { id: "brand", label: "Brand", defaultWidth: 150 },
  { id: "category", label: "Category", defaultWidth: 220 },
  { id: "own", label: "Our price", numeric: true, defaultWidth: 110 },
  { id: "lowest", label: "Lowest", numeric: true, defaultWidth: 110 },
  { id: "lowest_at", label: "Lowest at", defaultWidth: 130 },
];
const COMPETITOR_COL_WIDTH = 130;

function minColumnWidth(colId: string): number {
  if (colId === "name") return 320;
  if (colId === "category") return 180;
  if (colId === "brand" || colId === "lowest_at") return 120;
  if (colId === "sku" || colId.startsWith("comp:")) return 110;
  return MIN_COL_WIDTH;
}

function normalizeSavedWidths(widths: Record<string, number>): Record<string, number> {
  return Object.fromEntries(
    Object.entries(widths)
      .filter(([, width]) => Number.isFinite(width))
      .map(([colId, width]) => [colId, Math.max(minColumnWidth(colId), width)]),
  );
}

function toNum(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function fmtPrice(value: string | number | null | undefined, currency?: string | null): string {
  const n = toNum(value);
  if (n === null) return "—";
  const base = n.toLocaleString("bg-BG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency && currency !== "EUR" && currency !== "BGN" ? `${base} ${currency}` : base;
}

function fmtInt(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toLocaleString("bg-BG");
}

function bestLineByCompetitor(row: PriceComparisonRow): Map<UUID, CompetitorPriceLine> {
  const best = new Map<UUID, CompetitorPriceLine>();
  for (const line of row.competitor_prices) {
    if (!line.competitor_id) continue;
    const price = toNum(line.price);
    const current = best.get(line.competitor_id);
    const currentPrice = current ? toNum(current.price) : null;
    if (!current || (price !== null && (currentPrice === null || price < currentPrice))) {
      best.set(line.competitor_id, line);
    }
  }
  return best;
}

function lowestLine(row: PriceComparisonRow): CompetitorPriceLine | null {
  let winner: CompetitorPriceLine | null = null;
  let winnerPrice: number | null = null;
  for (const line of row.competitor_prices) {
    const price = toNum(line.price);
    if (price === null) continue;
    if (winnerPrice === null || price < winnerPrice) {
      winner = line;
      winnerPrice = price;
    }
  }
  return winner;
}

function loadLayout(): ColumnLayout {
  try {
    const raw = window.localStorage.getItem(LAYOUT_STORAGE_KEY);
    if (!raw) return DEFAULT_LAYOUT;
    const parsed = JSON.parse(raw) as Partial<ColumnLayout>;
    return {
      hidden: Array.isArray(parsed.hidden) ? parsed.hidden : [],
      pinned: Array.isArray(parsed.pinned) && parsed.pinned.length ? parsed.pinned : DEFAULT_LAYOUT.pinned,
      widths: parsed.widths && typeof parsed.widths === "object" ? normalizeSavedWidths(parsed.widths as Record<string, number>) : {},
    };
  } catch {
    return DEFAULT_LAYOUT;
  }
}

export default function ComparisonPage() {
  const [rows, setRows] = useState<PriceComparisonRow[]>([]);
  const [competitors, setCompetitors] = useState<ComparisonCompetitor[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [brandFilter, setBrandFilter] = useState("");
  const [competitorFilter, setCompetitorFilter] = useState("");
  const [hideOutOfStock, setHideOutOfStock] = useState(true);
  const [facets, setFacets] = useState<{ categories: string[]; brands: string[] }>({ categories: [], brands: [] });
  const [summary, setSummary] = useState<PriceComparisonSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [layout, setLayout] = useState<ColumnLayout>(DEFAULT_LAYOUT);
  const [columnsMenuOpen, setColumnsMenuOpen] = useState(false);
  const requestSeq = useRef(0);
  const resizeRef = useRef<{ colId: string; startX: number; startWidth: number } | null>(null);

  useEffect(() => {
    setLayout(loadLayout());
  }, []);

  useEffect(() => {
    api
      .get<{ categories: string[]; brands: string[] }>("/products/price-comparison-facets")
      .then(setFacets)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    api
      .get<PriceComparisonSummary>("/products/price-comparison-summary")
      .then(setSummary)
      .catch(() => undefined);
  }, []);

  const persistLayout = useCallback((next: ColumnLayout) => {
    setLayout(next);
    try {
      window.localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable */
    }
  }, []);

  useEffect(() => {
    const id = window.setTimeout(() => {
      setSearch(searchDraft.trim());
      setOffset(0);
    }, 400);
    return () => window.clearTimeout(id);
  }, [searchDraft]);

  const fetchPage = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
        only_matched: "true",
      });
      if (search) params.set("search", search);
      if (categoryFilter) params.set("category", categoryFilter);
      if (brandFilter) params.set("brand", brandFilter);
      if (competitorFilter) params.set("competitor_id", competitorFilter);
      if (hideOutOfStock) params.set("hide_out_of_stock", "true");
      const page = await api.get<PriceComparisonPageData>(`/products/price-comparison?${params}`);
      if (seq !== requestSeq.current) return;
      setRows(page.rows);
      setCompetitors(page.competitors);
      setTotal(page.total);
    } catch (err) {
      if (seq === requestSeq.current && !isApiAbortError(err)) {
        setError(err instanceof Error ? err.message : "Failed to load price comparison.");
      }
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [offset, search, categoryFilter, brandFilter, competitorFilter, hideOutOfStock]);

  useEffect(() => {
    void fetchPage();
  }, [fetchPage]);

  // ----- column model ------------------------------------------------------

  const allColumns = useMemo(() => {
    const base = BASE_COLUMNS.map((c) => ({ ...c, kind: "base" as const }));
    const comps = competitors.map((c) => ({
      id: `comp:${c.id}`,
      label: c.name,
      numeric: true,
      defaultWidth: COMPETITOR_COL_WIDTH,
      kind: "competitor" as const,
      competitor: c,
    }));
    return [...base, ...comps];
  }, [competitors]);

  const orderedVisible = useMemo(() => {
    const hidden = new Set(layout.hidden);
    const byId = new Map(allColumns.map((c) => [c.id, c]));
    const pinned = layout.pinned.map((id) => byId.get(id)).filter((c): c is (typeof allColumns)[number] => !!c && !hidden.has(c.id));
    const pinnedIds = new Set(pinned.map((c) => c.id));
    const rest = allColumns.filter((c) => !pinnedIds.has(c.id) && !hidden.has(c.id));
    return { pinned, rest, columns: [...pinned, ...rest] };
  }, [allColumns, layout]);

  const colWidth = useCallback(
    (colId: string) => {
      const fallback = allColumns.find((c) => c.id === colId)?.defaultWidth ?? COMPETITOR_COL_WIDTH;
      return Math.max(minColumnWidth(colId), layout.widths[colId] ?? fallback);
    },
    [layout.widths, allColumns],
  );

  const stickyOffsets = useMemo(() => {
    const offsets = new Map<string, number>();
    let acc = 0;
    for (const col of orderedVisible.pinned) {
      offsets.set(col.id, acc);
      acc += colWidth(col.id);
    }
    return offsets;
  }, [orderedVisible.pinned, colWidth]);

  const tableWidth = useMemo(
    () => orderedVisible.columns.reduce((sum, col) => sum + colWidth(col.id), 0),
    [orderedVisible.columns, colWidth],
  );

  // ----- column actions ----------------------------------------------------

  const togglePin = useCallback(
    (colId: string) => {
      const pinned = layout.pinned.includes(colId)
        ? layout.pinned.filter((id) => id !== colId)
        : [...layout.pinned, colId];
      persistLayout({ ...layout, pinned });
    },
    [layout, persistLayout],
  );

  const toggleHidden = useCallback(
    (colId: string) => {
      const hidden = layout.hidden.includes(colId)
        ? layout.hidden.filter((id) => id !== colId)
        : [...layout.hidden, colId];
      persistLayout({ ...layout, hidden });
    },
    [layout, persistLayout],
  );

  const startResize = useCallback(
    (colId: string, event: React.MouseEvent) => {
      event.preventDefault();
      event.stopPropagation();
      resizeRef.current = { colId, startX: event.clientX, startWidth: colWidth(colId) };

      const onMove = (e: MouseEvent) => {
        const state = resizeRef.current;
        if (!state) return;
        const width = Math.max(MIN_COL_WIDTH, state.startWidth + (e.clientX - state.startX));
        setLayout((prev) => ({ ...prev, widths: { ...prev.widths, [state.colId]: width } }));
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        const state = resizeRef.current;
        resizeRef.current = null;
        if (state) {
          setLayout((prev) => {
            try {
              window.localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(prev));
            } catch {
              /* ignore */
            }
            return prev;
          });
        }
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [colWidth],
  );

  // ----- cell rendering -----------------------------------------------------

  const renderCell = useCallback(
    (row: PriceComparisonRow, colId: string) => {
      const best = bestLineByCompetitor(row);
      const lowest = lowestLine(row);
      switch (colId) {
        case "sku":
          return row.sku;
        case "name":
          return row.name;
        case "brand":
          return row.brand ?? "—";
        case "category":
          return row.category ?? "—";
        case "own":
          return fmtPrice(row.own_price);
        case "lowest":
          return lowest ? fmtPrice(lowest.price, lowest.currency) : "—";
        case "lowest_at":
          return lowest ? lowest.competitor_name : "—";
        default: {
          const compId = colId.startsWith("comp:") ? colId.slice(5) : null;
          const line = compId ? best.get(compId) : undefined;
          if (!line) return "";
          const priceText = fmtPrice(line.price, line.currency);
          return line.url ? (
            <a href={line.url} target="_blank" rel="noreferrer" title={line.title ?? line.url}>
              {priceText}
            </a>
          ) : (
            priceText
          );
        }
      }
    },
    [],
  );

  const cellClass = useCallback(
    (row: PriceComparisonRow, colId: string, pinned: boolean): string => {
      const classes: string[] = [];
      if (pinned) classes.push("comparison-sticky");
      const col = allColumns.find((c) => c.id === colId);
      if (col?.numeric) classes.push("comparison-num");
      if (colId === "own") {
        classes.push("comparison-own");
        const own = toNum(row.own_price);
        const lowest = lowestLine(row);
        const lowestPrice = lowest ? toNum(lowest.price) : null;
        if (own !== null && lowestPrice !== null) classes.push(own <= lowestPrice ? "comparison-own-good" : "comparison-own-bad");
      }
      if (colId === "lowest") classes.push("comparison-min");
      if (colId === "name") classes.push("comparison-name-cell");
      if (colId.startsWith("comp:")) {
        const lowest = lowestLine(row);
        if (lowest?.competitor_id && `comp:${lowest.competitor_id}` === colId) classes.push("comparison-cell-min");
      }
      return classes.join(" ");
    },
    [allColumns],
  );

  const pageEnd = Math.min(offset + rows.length, total);
  const hiddenCount = layout.hidden.length;

  const resetFilters = () => {
    setSearchDraft("");
    setCategoryFilter("");
    setBrandFilter("");
    setCompetitorFilter("");
    setOffset(0);
  };

  const resetColumns = () => {
    persistLayout(DEFAULT_LAYOUT);
    setColumnsMenuOpen(false);
  };

  return (
    <div className="card comparison-card">
      <div className="stats-header" style={{ padding: "0.9rem 1rem 0.4rem" }}>
        <h2 style={{ margin: 0, fontSize: "1.05rem" }}>Price comparison</h2>
        <span className="muted" style={{ fontSize: "0.85rem" }}>
          Matched products only (confirmed + auto) · Records: {total.toLocaleString()}
        </span>
      </div>

      <div className="comparison-summary-strip" aria-label="Price comparison metrics">
        <div className="comparison-summary-tile">
          <span>Matched products</span>
          <strong>{fmtInt(summary?.matched_products)}</strong>
        </div>
        <div className="comparison-summary-tile">
          <span>Needs review</span>
          <strong>{fmtInt(summary?.needs_review)}</strong>
        </div>
        <div className="comparison-summary-tile">
          <span>Found URLs</span>
          <strong>{fmtInt(summary?.found_urls)}</strong>
        </div>
        <div className="comparison-summary-tile">
          <span>Tracked sites</span>
          <strong>{fmtInt(summary?.tracked_sites)}</strong>
        </div>
        <div className="comparison-summary-tile">
          <span>Scraped URLs</span>
          <strong>{fmtInt(summary?.scraped_urls)}</strong>
        </div>
      </div>

      <div className="comparison-toolbar">
        <input
          value={searchDraft}
          onChange={(e) => setSearchDraft(e.target.value)}
          placeholder="Search SKU, name, EAN, brand…"
          style={{ maxWidth: "18rem" }}
        />
        <select value={categoryFilter} onChange={(e) => { setCategoryFilter(e.target.value); setOffset(0); }}>
          <option value="">All categories</option>
          {facets.categories.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        <select value={brandFilter} onChange={(e) => { setBrandFilter(e.target.value); setOffset(0); }}>
          <option value="">All brands</option>
          {facets.brands.map((b) => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>
        <select value={competitorFilter} onChange={(e) => { setCompetitorFilter(e.target.value); setOffset(0); }}>
          <option value="">All competitors</option>
          {competitors.map((c) => (
            <option key={c.id} value={c.id}>Matched at {c.name}</option>
          ))}
        </select>
        <label className="inline-checkbox" style={{ fontSize: "0.84rem" }}>
          <input
            type="checkbox"
            checked={hideOutOfStock}
            onChange={(e) => { setHideOutOfStock(e.target.checked); setOffset(0); }}
          />
          Hide out of stock
        </label>
        {search || categoryFilter || brandFilter || competitorFilter ? (
          <button type="button" onClick={resetFilters}>Clear</button>
        ) : null}
        <span className="muted" style={{ fontSize: "0.82rem" }}>
          {loading ? "Loading…" : total === 0 ? "No results" : `${offset + 1}–${pageEnd} of ${total.toLocaleString()}`}
        </span>
        <span style={{ marginLeft: "auto", display: "inline-flex", gap: "0.4rem", position: "relative" }}>
          <button type="button" onClick={() => setColumnsMenuOpen((v) => !v)}>
            Columns{hiddenCount ? ` (${hiddenCount} hidden)` : ""}
          </button>
          {columnsMenuOpen ? (
            <div className="comparison-columns-menu">
              <button type="button" onClick={resetColumns}>
                Reset columns
              </button>
              {allColumns.map((col) => (
                <label key={col.id} className="inline-checkbox">
                  <input
                    type="checkbox"
                    checked={!layout.hidden.includes(col.id)}
                    disabled={col.id === "sku"}
                    onChange={() => toggleHidden(col.id)}
                  />
                  {col.label}
                  {layout.pinned.includes(col.id) ? <span className="muted"> · pinned</span> : null}
                </label>
              ))}
            </div>
          ) : null}
          <button type="button" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
            ‹ Prev
          </button>
          <button type="button" disabled={loading || pageEnd >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>
            Next ›
          </button>
        </span>
      </div>

      {error ? (
        <p className="section-inline-error" style={{ padding: "0 1rem 1rem" }}>
          {error}{" "}
          <button type="button" onClick={() => void fetchPage()}>Retry</button>
        </p>
      ) : null}

      <div className="comparison-scroll" onClick={() => columnsMenuOpen && setColumnsMenuOpen(false)}>
        <table className="workspace-table comparison-table" style={{ width: `max(100%, ${tableWidth}px)` }}>
          <colgroup>
            {orderedVisible.columns.map((col) => {
              const explicit = colWidth(col.id);
              return <col key={col.id} style={{ width: explicit, minWidth: explicit }} />;
            })}
          </colgroup>
          <thead>
            <tr>
              {orderedVisible.columns.map((col) => {
                const pinned = stickyOffsets.has(col.id);
                return (
                  <th
                    key={col.id}
                    className={pinned ? "comparison-sticky comparison-th" : "comparison-th"}
                    style={pinned ? { left: stickyOffsets.get(col.id) } : undefined}
                    title={col.kind === "competitor" ? col.competitor.domain : undefined}
                  >
                    <span className="comparison-th-label">{col.label}</span>
                    <span className="comparison-th-tools">
                      <button
                        type="button"
                        className={layout.pinned.includes(col.id) ? "comparison-tool comparison-tool-active" : "comparison-tool"}
                        title={layout.pinned.includes(col.id) ? "Unpin column" : "Pin column"}
                        onClick={() => togglePin(col.id)}
                      >
                        ⌖
                      </button>
                      {col.id !== "sku" ? (
                        <button type="button" className="comparison-tool" title="Hide column" onClick={() => toggleHidden(col.id)}>
                          ×
                        </button>
                      ) : null}
                    </span>
                    <span className="comparison-resizer" onMouseDown={(e) => startResize(col.id, e)} />
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.product_id}>
                {orderedVisible.columns.map((col) => {
                  const pinned = stickyOffsets.has(col.id);
                  return (
                    <td
                      key={col.id}
                      className={cellClass(row, col.id, pinned)}
                      style={pinned ? { left: stickyOffsets.get(col.id) } : undefined}
                      title={col.id === "name" ? row.name : undefined}
                    >
                      {renderCell(row, col.id)}
                    </td>
                  );
                })}
              </tr>
            ))}
            {!loading && rows.length === 0 && !error ? (
              <tr>
                <td colSpan={orderedVisible.columns.length} className="muted" style={{ textAlign: "center", padding: "2rem" }}>
                  {search || categoryFilter || brandFilter || competitorFilter ? (
                    "Nothing matches these filters."
                  ) : (
                    <>No confirmed or auto-matched products yet. Run matching from the <Link href="/competitors">workspace</Link>.</>
                  )}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}
