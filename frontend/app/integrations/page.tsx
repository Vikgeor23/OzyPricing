"use client";

import { useCallback, useEffect, useState } from "react";
import { API_BASE_URL } from "@/lib/config";
import { api, uploadXlsxImport, type ImportSummary } from "@/lib/api";
import type { Product } from "@/lib/types";

type ImportBatch = {
  id: string;
  filename: string;
  created_at: string;
  total_rows: number;
  imported_rows: number;
  skipped_rows: number;
  product_count: number;
};

type ProductListPage = {
  rows: Product[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

const PAGE_SIZE = 75;

function fmtDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, {
        day: "numeric",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}

function fmtMoney(v: string | number | null | undefined): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  return Number.isNaN(n) ? String(v) : n.toFixed(2);
}

export default function IntegrationsPage() {
  const [file, setFile] = useState<File | null>(null);
  const [fileInputKey, setFileInputKey] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportSummary | null>(null);
  const [importTaskId, setImportTaskId] = useState<string | null>(null);
  const [importProgress, setImportProgress] = useState<{ current: number; total: number } | null>(null);

  const [batches, setBatches] = useState<ImportBatch[]>([]);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const [openBatch, setOpenBatch] = useState<ImportBatch | null>(null);
  const [products, setProducts] = useState<Product[]>([]);
  const [productsTotal, setProductsTotal] = useState(0);
  const [productsOffset, setProductsOffset] = useState(0);
  const [productsLoading, setProductsLoading] = useState(false);

  const refreshBatches = useCallback(async () => {
    try {
      const rows = await api.get<ImportBatch[]>("/products/imports");
      setBatches(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load uploads");
    }
  }, []);

  useEffect(() => {
    void refreshBatches();
  }, [refreshBatches]);

  const loadBatchProducts = useCallback(async (batch: ImportBatch, offset: number) => {
    setProductsLoading(true);
    try {
      const page = await api.get<ProductListPage>(
        `/products/imports/${batch.id}/products?limit=${PAGE_SIZE}&offset=${offset}`,
      );
      setProducts(page.rows);
      setProductsTotal(page.total);
      setProductsOffset(page.offset);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load products");
    } finally {
      setProductsLoading(false);
    }
  }, []);

  function openUpload(batch: ImportBatch) {
    if (openBatch?.id === batch.id) {
      setOpenBatch(null);
      setProducts([]);
      return;
    }
    setOpenBatch(batch);
    setProducts([]);
    setProductsTotal(0);
    void loadBatchProducts(batch, 0);
  }

  async function deleteUpload(batch: ImportBatch) {
    const confirmed = window.confirm(
      `Delete "${batch.filename}"?\n\nThis also removes all ${batch.product_count.toLocaleString()} products from this upload, together with their links to competitor listings.`,
    );
    if (!confirmed) return;
    setDeletingId(batch.id);
    setError(null);
    try {
      await api.delete(`/products/imports/${batch.id}`);
      if (openBatch?.id === batch.id) {
        setOpenBatch(null);
        setProducts([]);
      }
      await refreshBatches();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
    } finally {
      setDeletingId(null);
    }
  }

  useEffect(() => {
    if (!importTaskId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const st = await api.get<{
          state: string;
          ready: boolean;
          current: number;
          total: number;
          result?: ImportSummary;
          error?: string;
        }>(`/products/import-tasks/${importTaskId}`);
        if (cancelled) return;
        setImportProgress({ current: st.current, total: st.total });
        if (st.ready) {
          setImportTaskId(null);
          setImportProgress(null);
          setUploading(false);
          if (st.error) {
            setError(st.error);
          } else if (st.result) {
            setResult(st.result);
          }
          await refreshBatches();
        }
      } catch {
        /* transient poll error — keep trying */
      }
    };
    void poll();
    const id = window.setInterval(() => void poll(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [importTaskId, refreshBatches]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setResult(null);
    if (!file) {
      setError("Choose a .xlsx file.");
      return;
    }
    if (!file.name.toLowerCase().endsWith(".xlsx")) {
      setError("The file must be .xlsx");
      return;
    }
    setUploading(true);
    setImportProgress(null);
    try {
      const queued = (await uploadXlsxImport("/products/import-xlsx", file)) as unknown as {
        task_id?: string;
      };
      if (queued && queued.task_id) {
        setImportTaskId(queued.task_id);
        setFile(null);
        setFileInputKey((k) => k + 1);
      } else {
        setError("Import queue returned no task id.");
        setUploading(false);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
      setUploading(false);
    }
  }

  return (
    <div>
      <h1 style={{ marginTop: 0 }}>Integrations</h1>
      <p className="muted" style={{ maxWidth: 760 }}>
        Upload your catalog as XLSX. Required columns: <strong>ean</strong>,{" "}
        <strong>manufacturer_code</strong> and <strong>name</strong> — everything else is optional and
        only improves matching.{" "}
        <a href={`${API_BASE_URL}/products/template-xlsx`} download style={{ fontWeight: 600 }}>
          Download example template (.xlsx)
        </a>
      </p>

      <div className="card" style={{ marginBottom: "1rem" }}>
        <h3 style={{ marginTop: 0, fontSize: "1rem" }}>New upload</h3>
        <form onSubmit={onSubmit} className="row">
          <div className="field" style={{ flex: "1 1 320px" }}>
            <label>Excel file (.xlsx)</label>
            <input
              key={fileInputKey}
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
          <button className="primary" type="submit" disabled={uploading || !file}>
            {uploading ? "Importing…" : "Upload & import"}
          </button>
        </form>
        {uploading ? (
          <div style={{ marginTop: "0.75rem" }}>
            <div className="stats-header">
              <span className="muted" style={{ fontSize: "0.85rem" }}>
                {importProgress && importProgress.total > 0
                  ? `${importProgress.current.toLocaleString()} / ${importProgress.total.toLocaleString()} rows`
                  : "Reading file…"}
              </span>
              <span className="muted" style={{ fontSize: "0.85rem" }}>
                {importProgress && importProgress.total > 0
                  ? `${Math.round((100 * importProgress.current) / importProgress.total)}%`
                  : ""}
              </span>
            </div>
            <div className="stat-bar" style={{ marginTop: "0.3rem" }}>
              <div
                className={
                  importProgress && importProgress.total > 0
                    ? "stat-bar-fill"
                    : "stat-bar-fill stat-bar-indeterminate"
                }
                style={{
                  width:
                    importProgress && importProgress.total > 0
                      ? `${Math.min(100, (100 * importProgress.current) / importProgress.total)}%`
                      : "40%",
                }}
              />
            </div>
          </div>
        ) : null}
        {error ? <p className="section-inline-error">{error}</p> : null}
        {result ? (
          <p className="muted" style={{ marginBottom: 0 }}>
            Imported: {result.imported_rows.toLocaleString()} · Skipped:{" "}
            {result.skipped_rows.toLocaleString()}
            {result.errors.length > 0 ? ` · Notes: ${result.errors.length}` : ""}
          </p>
        ) : null}
        {result && result.errors.length > 0 ? (
          <ul className="match-all-stats" style={{ marginTop: "0.5rem" }}>
            {result.errors.slice(0, 10).map((er, i) => (
              <li key={`${er.row}-${i}`}>
                Row {er.row}: {er.message}
              </li>
            ))}
            {result.errors.length > 10 ? <li>… and {result.errors.length - 10} more</li> : null}
          </ul>
        ) : null}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0, fontSize: "1rem" }}>Uploads</h3>
        {batches.length === 0 ? (
          <p className="muted" style={{ marginBottom: 0 }}>
            No uploads yet.
          </p>
        ) : (
          <div className="table-scroll">
            <table className="compact-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Uploaded</th>
                  <th>Products</th>
                  <th>Imported</th>
                  <th>Skipped</th>
                  <th style={{ width: 140 }} />
                </tr>
              </thead>
              <tbody>
                {batches.map((b) => (
                  <tr key={b.id} className={openBatch?.id === b.id ? "upload-row-open" : undefined}>
                    <td>
                      <button type="button" className="upload-name" onClick={() => openUpload(b)}>
                        {b.filename}
                      </button>
                    </td>
                    <td style={{ fontSize: "0.85rem" }}>{fmtDate(b.created_at)}</td>
                    <td>{b.product_count.toLocaleString()}</td>
                    <td>{b.imported_rows.toLocaleString()}</td>
                    <td>{b.skipped_rows.toLocaleString()}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <button type="button" onClick={() => openUpload(b)} style={{ marginRight: "0.4rem" }}>
                        {openBatch?.id === b.id ? "Hide" : "View"}
                      </button>
                      <button
                        type="button"
                        className="upload-delete"
                        disabled={deletingId !== null}
                        onClick={() => void deleteUpload(b)}
                      >
                        {deletingId === b.id ? "…" : "Delete"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {openBatch ? (
        <div className="card" style={{ marginTop: "1rem" }}>
          <div className="stats-header">
            <h3 style={{ margin: 0, fontSize: "1rem" }}>{openBatch.filename}</h3>
            <span className="muted" style={{ fontSize: "0.85rem" }}>
              {productsTotal === 0 && !productsLoading
                ? "No products"
                : `${productsOffset + 1}–${Math.min(productsOffset + products.length, productsTotal)} of ${productsTotal.toLocaleString()}`}
            </span>
          </div>
          {productsLoading ? (
            <p className="muted">Loading…</p>
          ) : (
            <div className="table-scroll" style={{ marginTop: "0.5rem" }}>
              <table className="compact-table">
                <thead>
                  <tr>
                    <th>EAN</th>
                    <th>Mfr code</th>
                    <th>Name</th>
                    <th>SKU</th>
                    <th>Brand</th>
                    <th>Category</th>
                    <th>Model</th>
                    <th>Own price</th>
                  </tr>
                </thead>
                <tbody>
                  {products.map((p) => (
                    <tr key={p.id}>
                      <td>{p.ean ?? "—"}</td>
                      <td>{p.manufacturer_code ?? "—"}</td>
                      <td style={{ maxWidth: 360 }}>{p.name}</td>
                      <td style={{ fontSize: "0.82rem" }}>{p.sku}</td>
                      <td>{p.brand ?? "—"}</td>
                      <td>{p.category ?? "—"}</td>
                      <td>{p.model ?? "—"}</td>
                      <td>{fmtMoney(p.own_price)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="workspace-pagination">
            <div className="workspace-pagination-actions">
              <button
                type="button"
                disabled={productsLoading || productsOffset === 0}
                onClick={() => void loadBatchProducts(openBatch, Math.max(0, productsOffset - PAGE_SIZE))}
              >
                Previous
              </button>
              <button
                type="button"
                disabled={productsLoading || productsOffset + PAGE_SIZE >= productsTotal}
                onClick={() => void loadBatchProducts(openBatch, productsOffset + PAGE_SIZE)}
              >
                Next
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
