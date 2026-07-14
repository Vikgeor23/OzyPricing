"""Cooperative cancellation flags for batch scrape tasks (Redis-backed).

The batch loop polls the flag between chunks, so a stop lands within one
id-batch (~a minute at current concurrency) and the task finishes cleanly
with committed results instead of being killed mid-write.
"""

from __future__ import annotations

import redis

from app.config import get_settings

_TTL_SEC = 24 * 3600


def _key(task_id: str) -> str:
    return f"pm:scrape:cancel:{task_id}"


def request_cancel(task_id: str) -> None:
    client = redis.Redis.from_url(get_settings().redis_url, socket_timeout=2)
    client.setex(_key(task_id), _TTL_SEC, "1")


def cancel_requested(task_id: str) -> bool:
    try:
        client = redis.Redis.from_url(get_settings().redis_url, socket_timeout=2)
        return client.get(_key(task_id)) is not None
    except Exception:  # noqa: BLE001
        return False
