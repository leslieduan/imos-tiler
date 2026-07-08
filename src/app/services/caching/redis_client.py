"""Lazy singleton Redis client for the CACHE_BACKEND=redis path.

One shared connection pool per process, reused by both L1 and L2. Connects
lazily (redis-py establishes connections per-command from the pool), so
importing this module has no side effect until a cache actually needs Redis.
"""

import os

import redis

_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    global _client
    if _client is None:
        url = os.environ["REDIS_URL"]
        # decode_responses=False: cached values are pickled bytes, not text.
        _client = redis.Redis.from_url(url, decode_responses=False)
    return _client
