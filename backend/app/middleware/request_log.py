"""Log incoming HTTP requests (method, path, Origin) during development."""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("app.request")


class DevRequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        origin = request.headers.get("origin") or "-"
        logger.info(
            "incoming request method=%s path=%s origin=%s",
            request.method,
            request.url.path,
            origin,
        )
        response = await call_next(request)
        return response
