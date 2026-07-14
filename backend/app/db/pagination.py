"""Shared list endpoint pagination limits."""

DEFAULT_PAGE_LIMIT = 75
MAX_PAGE_LIMIT = 100


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE_LIMIT))


def normalize_offset(offset: int) -> int:
    return max(0, offset)
