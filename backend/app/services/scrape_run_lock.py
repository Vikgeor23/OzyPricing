"""Per-competitor batch-scrape run registry (Redis-backed).

Prevents two users from unknowingly launching duplicate batch scrapes of the
same competitor: enqueue records the task id here, a second enqueue attaches
to the running task instead. The task releases the slot when it finishes; the
TTL is a backstop for tasks that die without cleanup (worker restart).
"""

from __future__ import annotations

import time

import redis

from app.config import get_settings

_TTL_SEC = 8 * 3600
# Long enough to outlive the Celery result backend's own expiry, so a task
# with a marker but no result is distinguishable from one we never queued.
_QUEUED_TTL_SEC = 48 * 3600


def _key(competitor_id: str) -> str:
    return f"pm:scrape:active:{competitor_id}"


def _client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, socket_timeout=2)


def active_task(competitor_id: str) -> str | None:
    try:
        raw = _client().get(_key(competitor_id))
        return raw.decode() if raw else None
    except Exception:  # noqa: BLE001
        return None


def mark_active(competitor_id: str, task_id: str) -> None:
    try:
        _client().setex(_key(competitor_id), _TTL_SEC, task_id)
    except Exception:  # noqa: BLE001
        pass
    mark_queued(task_id)


def mark_queued(task_id: str) -> None:
    """Timestamp the enqueue: Celery answers PENDING both for "waiting in the
    queue" and "never heard of it" (result expired / lost in a restart), so
    status endpoints need to know when we actually queued a task."""
    try:
        _client().setex(f"pm:task:queued_at:{task_id}", _QUEUED_TTL_SEC, repr(time.time()))
    except Exception:  # noqa: BLE001
        pass


def queued_at(task_id: str) -> float | None:
    try:
        raw = _client().get(f"pm:task:queued_at:{task_id}")
        return float(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def waiting_in_broker(task_id: str) -> bool:
    """True if the task message is still in the Celery broker queue (or
    prefetched but unacked) — i.e. no worker has started it yet. Returns False
    on any Redis error so callers fall back to the queued_at grace window."""
    try:
        client = _client()
        needle = task_id.encode()
        start = 0
        while start < 10_000:  # sanity cap; the queue is normally tiny
            chunk = client.lrange("celery", start, start + 499)
            if not chunk:
                break
            if any(needle in msg for msg in chunk):
                return True
            if len(chunk) < 500:
                break
            start += 500
        return any(needle in msg for msg in client.hvals("unacked"))
    except Exception:  # noqa: BLE001
        return False


def mark_owner(task_id: str, owner_email: str) -> None:
    """Remember which account launched a run (for per-account activity lists)."""
    try:
        _client().setex(f"pm:scrape:owner:{task_id}", _TTL_SEC, owner_email)
    except Exception:  # noqa: BLE001
        pass


def owner_of(task_id: str) -> str | None:
    try:
        raw = _client().get(f"pm:scrape:owner:{task_id}")
        return raw.decode() if raw else None
    except Exception:  # noqa: BLE001
        return None


def mark_run(kind: str, competitor_id: str, task_id: str) -> None:
    """Visibility registry for non-scrape runs (match / discovery): the widget
    lists them across devices. No dedup semantics — scrape keeps its own lock."""
    try:
        _client().setex(f"pm:run:{kind}:{competitor_id}", _TTL_SEC, task_id)
    except Exception:  # noqa: BLE001
        pass
    mark_queued(task_id)


def list_runs(kind: str) -> dict[str, str]:
    try:
        client = _client()
        out: dict[str, str] = {}
        for key in client.scan_iter(match=f"pm:run:{kind}:*", count=200):
            raw = client.get(key)
            if raw:
                out[key.decode().rsplit(":", 1)[-1]] = raw.decode()
        return out
    except Exception:  # noqa: BLE001
        return {}


def list_active() -> dict[str, str]:
    """All registered runs: {competitor_id: task_id}. Liveness is not checked here."""
    try:
        client = _client()
        out: dict[str, str] = {}
        for key in client.scan_iter(match="pm:scrape:active:*", count=200):
            raw = client.get(key)
            if raw:
                out[key.decode().rsplit(":", 1)[-1]] = raw.decode()
        return out
    except Exception:  # noqa: BLE001
        return {}


def release(competitor_id: str, task_id: str) -> None:
    """Clear the slot only if it still belongs to this task."""
    try:
        client = _client()
        raw = client.get(_key(competitor_id))
        if raw and raw.decode() == task_id:
            client.delete(_key(competitor_id))
    except Exception:  # noqa: BLE001
        pass
