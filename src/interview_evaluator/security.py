"""API-key auth and per-IP rate limiting, both as FastAPI dependencies."""

import math
import os
import secrets
import threading
import time
from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, APIKeyQuery

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
api_key_query = APIKeyQuery(name="key", auto_error=False)


def get_service_api_key() -> str | None:
    """The configured key; None disables auth (local dev)."""
    return os.environ.get("SERVICE_API_KEY") or None


def require_api_key(
    key: str | None = Security(api_key_header),
    expected: str | None = Depends(get_service_api_key),
) -> None:
    if expected is None:
        return
    if key is None or not secrets.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(
            401, "Missing or invalid API key: send it in the X-API-Key header",
            headers={"WWW-Authenticate": "APIKey"},
        )


def require_api_key_header_or_query(
    header_key: str | None = Security(api_key_header),
    query_key: str | None = Security(api_key_query),
    expected: str | None = Depends(get_service_api_key),
) -> None:
    """Only for SSE endpoints: browsers' EventSource can't send headers, so `?key=` is accepted."""
    require_api_key(header_key or query_key, expected)


class RateLimiter:
    """In-memory token bucket per client key: `per_minute` burst, refilled continuously.

    In-memory state is why the deployment is pinned to one replica.
    """

    MAX_TRACKED = 10_000

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        if per_minute < 1:
            raise ValueError("RATE_LIMIT_PER_MIN must be at least 1")
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0  # tokens per second
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill time)
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Takes one token; returns None if allowed, else seconds until a token is available."""
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return (1 - tokens) / self.rate
            self._buckets[key] = (tokens - 1, now)
            if len(self._buckets) > self.MAX_TRACKED:
                self._prune(now)
            return None

    def _prune(self, now: float) -> None:
        # Drop buckets that have refilled completely; they are indistinguishable from new ones.
        full_after = self.capacity / self.rate
        self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < full_after}


@lru_cache
def get_rate_limiter() -> RateLimiter:
    return RateLimiter(int(os.environ.get("RATE_LIMIT_PER_MIN", "10")))


def client_ip(request: Request) -> str:
    """First X-Forwarded-For hop (set by Container Apps ingress), else the socket peer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, limiter: RateLimiter = Depends(get_rate_limiter)) -> None:
    retry_after = limiter.hit(client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            429,
            "Rate limit exceeded, retry later",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )
