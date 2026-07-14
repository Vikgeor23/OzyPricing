import { API_BASE_URL } from "./config";

const AUTH_TOKEN_KEY = "pm_auth_token";
const AUTH_EMAIL_KEY = "pm_auth_email";

export function getAuthToken(): string | null {
  try {
    return window.localStorage.getItem(AUTH_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function getAuthEmail(): string | null {
  try {
    return window.localStorage.getItem(AUTH_EMAIL_KEY);
  } catch {
    return null;
  }
}

export function setAuthSession(token: string, email: string): void {
  try {
    window.localStorage.setItem(AUTH_TOKEN_KEY, token);
    window.localStorage.setItem(AUTH_EMAIL_KEY, email);
  } catch {
    /* storage unavailable */
  }
}

export function clearAuthSession(): void {
  try {
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    window.localStorage.removeItem(AUTH_EMAIL_KEY);
  } catch {
    /* ignore */
  }
}

function authHeader(): Record<string, string> {
  const token = typeof window === "undefined" ? null : getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function redirectToLoginOn401(path: string, status?: number): void {
  if (status !== 401 || path.startsWith("/auth/")) return;
  if (typeof window === "undefined") return;
  clearAuthSession();
  if (window.location.pathname !== "/login") {
    window.location.href = "/login";
  }
}

export class ApiError extends Error {
  readonly apiBaseUrl: string;
  readonly path: string;
  readonly url: string;
  readonly status?: number;

  constructor(options: {
    message: string;
    apiBaseUrl: string;
    path: string;
    status?: number;
    cause?: unknown;
  }) {
    super(options.message);
    this.name = "ApiError";
    this.apiBaseUrl = options.apiBaseUrl;
    this.path = options.path;
    this.url = buildUrl(options.path);
    this.status = options.status;
    if (options.cause !== undefined) {
      this.cause = options.cause;
    }
  }
}

/** Thrown when a request is aborted (navigation, Strict Mode cleanup, explicit cancel). */
export class ApiAbortError extends Error {
  readonly path: string;

  constructor(path: string, cause?: unknown) {
    super("Request aborted");
    this.name = "ApiAbortError";
    this.path = path;
    if (cause !== undefined) {
      this.cause = cause;
    }
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError;
}

export function isApiAbortError(err: unknown): err is ApiAbortError {
  return err instanceof ApiAbortError;
}

/** Narrow JSON body when `api.post` may return `void` (e.g. empty/204). */
export function isApiJsonBody<T extends object>(value: T | void | undefined): value is T {
  return value !== undefined && value !== null && typeof value === "object";
}

export type QueuedTaskResponse = { task_id: string; message?: string };

export function readQueuedTaskResponse(
  value: QueuedTaskResponse | void | undefined,
): QueuedTaskResponse | null {
  if (isApiJsonBody(value) && "task_id" in value && typeof value.task_id === "string") {
    return value;
  }
  return null;
}

export function isAbortLike(err: unknown): boolean {
  if (isApiAbortError(err)) return true;
  if (err instanceof DOMException && err.name === "AbortError") return true;
  if (err instanceof Error && err.name === "AbortError") return true;
  return false;
}

function isNetworkFailure(err: unknown): boolean {
  if (isAbortLike(err)) return false;
  if (err instanceof TypeError) return true;
  if (err instanceof ApiError && err.status == null) return true;
  return false;
}

/** Short message for inline section errors (not the full-page banner). */
export function getSectionErrorMessage(err: unknown): string {
  if (isApiAbortError(err)) return "";
  if (isApiError(err)) {
    if (err.status) return err.message;
    return "Could not load data. Try again or check API status in the top bar.";
  }
  if (err instanceof Error) {
    if (err.message === "Failed to fetch") {
      return "Network error. Try again or check API status in the top bar.";
    }
    return err.message;
  }
  return "Request failed";
}

function normalizePath(path: string): string {
  return path.startsWith("/") ? path : `/${path}`;
}

function buildUrl(path: string): string {
  return `${API_BASE_URL}${normalizePath(path)}`;
}

function networkErrorMessage(path: string, cause: unknown): string {
  const hint = `Cannot reach the API at ${API_BASE_URL}. Confirm the backend is running and open ${API_BASE_URL}/docs in your browser.`;
  if (cause instanceof TypeError) {
    return `${hint} (${cause.message})`;
  }
  return hint;
}

async function parseResponse<T>(res: Response, path: string): Promise<T | void> {
  if (res.status === 204) {
    return;
  }
  if (!res.ok) {
    const text = await res.text();
    let message = text || res.statusText || `HTTP ${res.status}`;
    try {
      const parsed = JSON.parse(text) as { detail?: string | unknown };
      if (typeof parsed.detail === "string") {
        message = parsed.detail;
      } else if (parsed.detail != null) {
        message = JSON.stringify(parsed.detail);
      }
    } catch {
      // keep raw text
    }
    redirectToLoginOn401(path, res.status);
    throw new ApiError({
      message,
      apiBaseUrl: API_BASE_URL,
      path,
      status: res.status,
    });
  }
  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return (await res.json()) as T;
  }
  return;
}

export type HttpMethod = "GET" | "POST" | "PUT" | "DELETE";

export type ApiRequestOptions = {
  signal?: AbortSignal;
  /** Skip the automatic GET retry on network failure. */
  skipRetry?: boolean;
};

function fetchOptions(method: HttpMethod, body?: unknown, signal?: AbortSignal): RequestInit {
  return {
    method,
    mode: "cors",
    credentials: "omit",
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...authHeader(),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
    signal,
  };
}

const GET_RETRY_DELAY_MS = 300;

async function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ApiAbortError("", signal.reason));
      return;
    }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new ApiAbortError("", signal.reason));
      },
      { once: true },
    );
  });
}

async function fetchOnce<T>(path: string, init: RequestInit): Promise<T | void> {
  const res = await fetch(buildUrl(path), init);
  return parseResponse<T>(res, path);
}

async function fetchWithGetRetry<T>(
  path: string,
  init: RequestInit,
  options?: ApiRequestOptions,
): Promise<T | void> {
  const method = init.method ?? "GET";

  try {
    return await fetchOnce<T>(path, init);
  } catch (err) {
    if (isAbortLike(err)) {
      throw new ApiAbortError(path, err);
    }
    if (err instanceof ApiError) {
      throw err;
    }

    const shouldRetry =
      method === "GET" &&
      !options?.skipRetry &&
      isNetworkFailure(err) &&
      !init.signal?.aborted;

    if (!shouldRetry) {
      throw err;
    }

    await sleep(GET_RETRY_DELAY_MS, init.signal ?? undefined);
    return fetchOnce<T>(path, init);
  }
}

function wrapRequestError(path: string, err: unknown): never {
  if (isAbortLike(err)) {
    throw new ApiAbortError(path, err);
  }
  if (err instanceof ApiError) {
    throw err;
  }
  const message = networkErrorMessage(path, err);
  if (process.env.NODE_ENV === "development") {
    console.error("[api] request failed", { path, apiBaseUrl: API_BASE_URL, err });
  }
  throw new ApiError({
    message,
    apiBaseUrl: API_BASE_URL,
    path,
    cause: err,
  });
}

export type HealthResponse = { status: string };

export type HealthCheckOptions = {
  signal?: AbortSignal;
};

/** Lightweight connectivity probe — hits GET /health with explicit CORS + no credentials. */
export async function checkApiHealth(options?: HealthCheckOptions): Promise<HealthResponse> {
  const path = "/health";
  try {
    const res = await fetch(buildUrl(path), {
      method: "GET",
      mode: "cors",
      credentials: "omit",
      cache: "no-store",
      signal: options?.signal,
    });
    if (!res.ok) {
      throw new ApiError({
        message: `Health check failed: HTTP ${res.status}`,
        apiBaseUrl: API_BASE_URL,
        path,
        status: res.status,
      });
    }
    return (await res.json()) as HealthResponse;
  } catch (err) {
    if (isAbortLike(err)) {
      throw new ApiAbortError(path, err);
    }
    if (err instanceof ApiError) {
      throw err;
    }
    throw new ApiError({
      message: networkErrorMessage(path, err),
      apiBaseUrl: API_BASE_URL,
      path,
      cause: err,
    });
  }
}

export async function apiRequest<T>(
  path: string,
  method: HttpMethod = "GET",
  body?: unknown,
  options?: ApiRequestOptions,
): Promise<T | void> {
  const init = fetchOptions(method, body, options?.signal);

  try {
    return await fetchWithGetRetry<T>(path, init, options);
  } catch (err) {
    wrapRequestError(path, err);
  }
}

export const api = {
  get: <T>(path: string, options?: ApiRequestOptions) =>
    apiRequest<T>(path, "GET", undefined, options) as Promise<T>,
  post: <T>(path: string, body?: unknown, options?: ApiRequestOptions) =>
    apiRequest<T>(path, "POST", body, options) as Promise<T | void>,
  put: <T>(path: string, body: unknown, options?: ApiRequestOptions) =>
    apiRequest<T>(path, "PUT", body, options) as Promise<T>,
  delete: (path: string, options?: ApiRequestOptions) => apiRequest<void>(path, "DELETE", undefined, options),
};

export async function downloadApiFile(path: string, filename: string, options?: ApiRequestOptions): Promise<void> {
  const normalizedPath = normalizePath(path);
  try {
    const res = await fetch(buildUrl(normalizedPath), {
      method: "GET",
      mode: "cors",
      credentials: "omit",
      headers: authHeader(),
      cache: "no-store",
      signal: options?.signal,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new ApiError({
        message: text || res.statusText || `HTTP ${res.status}`,
        apiBaseUrl: API_BASE_URL,
        path: normalizedPath,
        status: res.status,
      });
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    if (isAbortLike(err)) {
      throw new ApiAbortError(normalizedPath, err);
    }
    if (err instanceof ApiError) {
      throw err;
    }
    wrapRequestError(normalizedPath, err);
  }
}

export type ImportSummary = {
  total_rows: number;
  imported_rows: number;
  skipped_rows: number;
  errors: { row: number; message: string }[];
};

export async function uploadXlsxImport(path: string, file: File, options?: ApiRequestOptions): Promise<ImportSummary> {
  const normalizedPath = normalizePath(path);
  const url = buildUrl(normalizedPath);
  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch(url, {
      method: "POST",
      mode: "cors",
      credentials: "omit",
      headers: authHeader(),
      body: formData,
      cache: "no-store",
      signal: options?.signal,
    });
    if (!res.ok) {
      const text = await res.text();
      redirectToLoginOn401(normalizedPath, res.status);
      throw new ApiError({
        message: text || res.statusText || `HTTP ${res.status}`,
        apiBaseUrl: API_BASE_URL,
        path: normalizedPath,
        status: res.status,
      });
    }
    return (await res.json()) as ImportSummary;
  } catch (err) {
    if (isAbortLike(err)) {
      throw new ApiAbortError(normalizedPath, err);
    }
    if (err instanceof ApiError) {
      throw err;
    }
    wrapRequestError(normalizedPath, err);
  }
}

/** @deprecated Use `API_BASE_URL` from `@/lib/config` */
export const API_BASE = API_BASE_URL;
