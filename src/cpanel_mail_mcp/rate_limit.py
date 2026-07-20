"""Per-token rate limiting (sliding window, in-memory).

Two buckets: `send` (SMTP/APPEND-heavy) and `read` (IMAP fetch/search).
Limits are per **caller identity** — bearer token for opaque-bearer mode,
JWT email for OAuth mode. Configurable via env vars, so operators can
tighten limits if abuse shows up in logs.

In-memory ⇒ resets on restart and doesn't share across replicas. Fine for
a single-node LXC. For multi-node, swap out for Redis.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    max_per_window: int
    window_seconds: int
    calls: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def configure(self, bucket: str, max_per_window: int, window_seconds: int) -> None:
        with self._lock:
            self._buckets[bucket] = _Bucket(
                max_per_window=max_per_window, window_seconds=window_seconds
            )

    def check(self, bucket: str, identity: str) -> None:
        """Raise `RateLimited` if `identity` has exceeded the bucket's window."""
        from .errors import RateLimited

        with self._lock:
            b = self._buckets.get(bucket)
            if b is None:
                return  # bucket not configured → no limit
            now = time.monotonic()
            cutoff = now - b.window_seconds
            q = b.calls[identity]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= b.max_per_window:
                retry_after = max(1, int(q[0] + b.window_seconds - now))
                raise RateLimited(retry_after, bucket)
            q.append(now)


limiter = RateLimiter()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def init_from_env() -> None:
    """Set limits from `MCP_RATE_LIMIT_*` env vars (or defaults)."""
    limiter.configure(
        "send",
        _int_env("MCP_RATE_LIMIT_SEND_PER_MIN", 30),
        60,
    )
    limiter.configure(
        "read",
        _int_env("MCP_RATE_LIMIT_READ_PER_MIN", 300),
        60,
    )
