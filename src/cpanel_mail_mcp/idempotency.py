"""In-memory idempotency store for `send` calls.

Client provides `idempotency_key` (arbitrary string). If the same
(identity, key) pair is seen within the TTL, we return the cached result
of the earlier call instead of re-sending. Prevents duplicate email
delivery on retries after client timeout.

Not persisted — restarts wipe the cache, so a retry after a server
restart WILL re-send. That's acceptable: the alternative (persisting)
is way more infrastructure for a rare edge case.
"""
from __future__ import annotations

import threading
import time


class IdempotencyStore:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], tuple[float, dict]] = {}

    def _prune(self, now: float) -> None:
        cutoff = now - self.ttl
        stale = [k for k, (t, _) in self._entries.items() if t < cutoff]
        for k in stale:
            del self._entries[k]

    def get(self, identity: str, key: str) -> dict | None:
        if not key:
            return None
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            entry = self._entries.get((identity, key))
            return entry[1] if entry else None

    def put(self, identity: str, key: str, result: dict) -> None:
        if not key:
            return
        with self._lock:
            self._entries[(identity, key)] = (time.monotonic(), result)


store = IdempotencyStore()
