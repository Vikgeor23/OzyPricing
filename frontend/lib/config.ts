/**
 * Browser-reachable API origin.
 *
 * Must be a URL the user's browser can resolve (e.g. http://localhost:8000).
 * Do NOT set http://backend:8000 here — Docker service names are not valid in the browser.
 */
const raw = process.env.NEXT_PUBLIC_API_URL?.trim();

export const API_BASE_URL = raw && raw.length > 0 ? raw.replace(/\/$/, "") : "http://localhost:8000";

if (typeof window !== "undefined" && /:\/\/backend[:/]/i.test(API_BASE_URL)) {
  console.warn(
    "[config] NEXT_PUBLIC_API_URL points at a Docker internal host (%s). Use http://localhost:8000 for browser requests.",
    API_BASE_URL,
  );
}
