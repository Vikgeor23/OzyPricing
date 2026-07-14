"""Require a valid bearer token for every API route except the public set."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.database import SessionLocal
from app.services.auth_service import token_is_valid

# Prefixes that stay reachable without a token: auth itself, liveness probe,
# OpenAPI docs, and the public XLSX template download (plain <a> links can't
# attach Authorization headers).
_PUBLIC_PREFIXES = (
    "/auth/",
    "/api/auth/",
    "/health",
    "/api/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/products/template-xlsx",
    "/api/products/template-xlsx",
)


def _is_public(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in _PUBLIC_PREFIXES)


class AuthRequiredMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or _is_public(request.url.path):
            return await call_next(request)

        authorization = request.headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if token:
            db = SessionLocal()
            try:
                if token_is_valid(db, token):
                    return await call_next(request)
            finally:
                db.close()
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
