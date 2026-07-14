"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.middleware.auth_required import AuthRequiredMiddleware
from app.middleware.request_log import DevRequestLogMiddleware
from app.routers import (
    auth,
    competitor_categories,
    competitor_products,
    competitors,
    dashboard,
    debug,
    jobs,
    matches,
    prices,
    products,
)

# Cloudflare sends full paths (/api/...) — always mirror routes under /api.
API_MOUNT_PREFIX = "/api"

APP_ROUTERS = (
    auth.router,
    matches.router,
    products.router,
    competitors.router,
    competitor_categories.router,
    competitor_products.router,
    prices.router,
    jobs.router,
    dashboard.router,
    debug.router,
)


def health() -> dict[str, str]:
    """Lightweight liveness probe (no DB). Used by frontend connectivity checks."""
    return {"status": "ok"}


def _include_routers(application: FastAPI, *, prefix: str = "") -> None:
    for router in APP_ROUTERS:
        application.include_router(router, prefix=prefix)


def _register_health_routes(application: FastAPI, *, prefix: str = "") -> None:
    path = f"{prefix}/health" if prefix else "/health"
    application.add_api_route(
        path,
        health,
        methods=["GET"],
        tags=["health"],
        name=f"health{prefix.replace('/', '_') or '_root'}",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build FastAPI app; optional ``settings`` override for tests."""
    cfg = settings or get_settings()

    if cfg.debug:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    application = FastAPI(title=cfg.app_name, debug=cfg.debug)

    origins = [o.strip() for o in cfg.cors_origins.split(",") if o.strip()]

    # Added before CORS so CORS stays the outermost layer (401 responses must
    # still carry CORS headers for the browser to read them).
    application.add_middleware(AuthRequiredMiddleware)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins if origins else ["http://localhost:3000"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=600,
    )

    if cfg.debug:
        application.add_middleware(DevRequestLogMiddleware)

    # Unprefixed routes (local dev: http://localhost:8000/health, /competitors/...)
    _include_routers(application)
    _register_health_routes(application)

    # Prefixed aliases for production proxy (https://host/api/health, /api/competitors/...)
    _include_routers(application, prefix=API_MOUNT_PREFIX)
    _register_health_routes(application, prefix=API_MOUNT_PREFIX)

    logging.getLogger("app.request").info(
        "CORS allow_origins=%s allow_credentials=False api_mount=%s",
        origins if origins else ["http://localhost:3000"],
        API_MOUNT_PREFIX,
    )
    return application


app = create_app()
