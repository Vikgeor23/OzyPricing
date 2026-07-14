"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { checkApiHealth, isApiAbortError, isApiError } from "@/lib/api";

export type ApiHealthState = "checking" | "ok" | "error";

type ApiHealthContextValue = {
  state: ApiHealthState;
  /** True only when GET /health returned `{ status: "ok" }`. */
  healthy: boolean;
  detail: string | null;
  recheck: () => Promise<void>;
};

const ApiHealthContext = createContext<ApiHealthContextValue | null>(null);

export function ApiHealthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<ApiHealthState>("checking");
  const [detail, setDetail] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const recheck = useCallback(async () => {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setState("checking");
    setDetail(null);

    try {
      const res = await checkApiHealth({ signal: ac.signal });
      if (ac.signal.aborted) return;
      if (res.status === "ok") {
        setState("ok");
        setDetail(null);
      } else {
        setState("error");
        setDetail(`Unexpected response: ${JSON.stringify(res)}`);
      }
    } catch (err) {
      if (isApiAbortError(err)) return;
      setState("error");
      if (isApiError(err)) {
        setDetail(err.message);
      } else {
        setDetail(err instanceof Error ? err.message : "Health check failed");
      }
    }
  }, []);

  useEffect(() => {
    void recheck();
    return () => abortRef.current?.abort();
  }, [recheck]);

  return (
    <ApiHealthContext.Provider
      value={{
        state,
        healthy: state === "ok",
        detail,
        recheck,
      }}
    >
      {children}
    </ApiHealthContext.Provider>
  );
}

export function useApiHealth(): ApiHealthContextValue {
  const ctx = useContext(ApiHealthContext);
  if (ctx == null) {
    throw new Error("useApiHealth must be used within ApiHealthProvider");
  }
  return ctx;
}
