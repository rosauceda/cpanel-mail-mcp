"""RateLimiter sliding-window + Idempotency store TTL behaviors."""
import time
import pytest

from cpanel_mail_mcp.errors import RateLimited
from cpanel_mail_mcp.rate_limit import RateLimiter
from cpanel_mail_mcp.idempotency import IdempotencyStore


# ── rate limiter ───────────────────────────────────────────────────────


def test_rate_limiter_allows_within_window():
    rl = RateLimiter()
    rl.configure("send", max_per_window=3, window_seconds=60)
    for _ in range(3):
        rl.check("send", "alice")


def test_rate_limiter_blocks_over_limit():
    rl = RateLimiter()
    rl.configure("send", max_per_window=2, window_seconds=60)
    rl.check("send", "alice")
    rl.check("send", "alice")
    with pytest.raises(RateLimited) as ei:
        rl.check("send", "alice")
    assert ei.value.code == "rate_limited"
    assert "send" in ei.value.error


def test_rate_limiter_isolates_identities():
    rl = RateLimiter()
    rl.configure("send", max_per_window=1, window_seconds=60)
    rl.check("send", "alice")
    with pytest.raises(RateLimited):
        rl.check("send", "alice")
    # bob is unaffected
    rl.check("send", "bob")


def test_rate_limiter_isolates_buckets():
    rl = RateLimiter()
    rl.configure("send", max_per_window=1, window_seconds=60)
    rl.configure("read", max_per_window=1, window_seconds=60)
    rl.check("send", "alice")
    rl.check("read", "alice")   # different bucket → allowed


def test_rate_limiter_noop_when_bucket_not_configured():
    rl = RateLimiter()
    # unconfigured "send" bucket → check is a no-op
    rl.check("send", "alice")


# ── idempotency store ──────────────────────────────────────────────────


def test_idempotency_returns_cached_result():
    s = IdempotencyStore(ttl_seconds=60)
    result = {"ok": True, "message_id": "<m1@x>"}
    assert s.get("alice", "k1") is None
    s.put("alice", "k1", result)
    assert s.get("alice", "k1") == result


def test_idempotency_isolates_identities():
    s = IdempotencyStore(ttl_seconds=60)
    s.put("alice", "k1", {"who": "alice"})
    assert s.get("bob", "k1") is None


def test_idempotency_empty_key_never_caches():
    s = IdempotencyStore(ttl_seconds=60)
    s.put("alice", "", {"x": 1})
    assert s.get("alice", "") is None


def test_idempotency_prunes_expired():
    s = IdempotencyStore(ttl_seconds=1)
    s.put("alice", "k", {"x": 1})
    time.sleep(1.1)
    assert s.get("alice", "k") is None
