"""Selects the L1/L2 cache backend via the CACHE_BACKEND env var.

- "memory" (default): today's behavior, an in-process TTLCache + Memoizer.
- "redis": shared cache + cross-instance single-flight dedup via ElastiCache,
  for ECS deployments running more than one instance.
- "none": bypass caching entirely — every call recomputes.
"""

import os
from collections.abc import MutableMapping

from app.services.caching.memoizer import CacheBackend, Memoizer, NullMemoizer, RedisMemoizer
from app.services.caching.redis_client import get_redis_client

_REDIS_LOCK_TTL_SECONDS = int(os.environ.get("REDIS_LOCK_TTL_SECONDS", 30))
_REDIS_WAIT_TIMEOUT_SECONDS = float(os.environ.get("REDIS_WAIT_TIMEOUT_SECONDS", 15))


def create_memoizer(
    *, namespace: str, memory_cache: MutableMapping, ttl_seconds: int
) -> CacheBackend:
    backend = os.environ.get("CACHE_BACKEND", "memory")
    if backend == "memory":
        return Memoizer(memory_cache)
    if backend == "redis":
        return RedisMemoizer(
            get_redis_client(),
            namespace=namespace,
            ttl_seconds=ttl_seconds,
            lock_ttl_seconds=_REDIS_LOCK_TTL_SECONDS,
            wait_timeout=_REDIS_WAIT_TIMEOUT_SECONDS,
        )
    if backend == "none":
        return NullMemoizer()
    raise ValueError(f"Unknown CACHE_BACKEND: {backend!r} (expected memory, redis, or none)")
