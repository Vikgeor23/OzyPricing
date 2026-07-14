"use client";

import { useApiHealth } from "@/contexts/ApiHealthContext";

export function ApiHealthStatus() {
  const { state, detail, recheck } = useApiHealth();

  const dotClass =
    state === "ok"
      ? "api-health-dot api-health-dot-ok"
      : state === "error"
        ? "api-health-dot api-health-dot-error"
        : "api-health-dot api-health-dot-checking";

  const label = state === "checking" ? "Connecting…" : state === "ok" ? "Connected" : "Offline";

  return (
    <div className="api-health">
      <button
        type="button"
        className="api-health-row"
        onClick={() => recheck()}
        disabled={state === "checking"}
        title={state === "error" && detail ? detail : "Click to re-check the connection"}
      >
        <span className={dotClass} aria-hidden />
        <span>{label}</span>
      </button>
    </div>
  );
}
