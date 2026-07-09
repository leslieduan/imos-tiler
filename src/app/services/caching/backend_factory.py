"""Selects the L1/L2 cache backend via the CACHE_BACKEND setting.

- "none" (default): bypass caching entirely — every call recomputes.
- "redis": shared cache + cross-instance single-flight dedup via ElastiCache,
  for deployments running more than one instance.
"""

from app.config import settings
from app.services.caching.memoizer import CacheBackend, NullMemoizer, RedisMemoizer
from app.services.caching.redis_client import get_redis_client


def create_memoizer(*, namespace: str, ttl_seconds: int) -> CacheBackend:
    backend = settings.CACHE_BACKEND
    if backend == "redis":
        return RedisMemoizer(
            get_redis_client(),
            namespace=namespace,
            ttl_seconds=ttl_seconds,
            lock_ttl_seconds=settings.REDIS_LOCK_TTL_SECONDS,
            wait_timeout=settings.REDIS_WAIT_TIMEOUT_SECONDS,
        )
    if backend == "none":
        return NullMemoizer()
    raise ValueError(f"Unknown CACHE_BACKEND: {backend!r} (expected redis or none)")
