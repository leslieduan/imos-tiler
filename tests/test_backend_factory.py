import fakeredis
import pytest

from app.services.caching.backend_factory import create_memoizer
from app.services.caching.memoizer import Memoizer, NullMemoizer, RedisMemoizer


def test_defaults_to_memory_backend(monkeypatch):
    monkeypatch.delenv("CACHE_BACKEND", raising=False)
    memo = create_memoizer(namespace="l1", memory_cache={}, ttl_seconds=60)
    assert isinstance(memo, Memoizer)


def test_explicit_memory_backend(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "memory")
    cache: dict = {}
    memo = create_memoizer(namespace="l1", memory_cache=cache, ttl_seconds=60)
    assert isinstance(memo, Memoizer)
    assert memo.cache is cache


def test_none_backend(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "none")
    memo = create_memoizer(namespace="l1", memory_cache={}, ttl_seconds=60)
    assert isinstance(memo, NullMemoizer)


def test_redis_backend(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(
        "app.services.caching.backend_factory.get_redis_client",
        lambda: fakeredis.FakeStrictRedis(),
    )
    memo = create_memoizer(namespace="l1", memory_cache={}, ttl_seconds=60)
    assert isinstance(memo, RedisMemoizer)
    assert memo.namespace == "l1"


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "disk")
    with pytest.raises(ValueError, match="disk"):
        create_memoizer(namespace="l1", memory_cache={}, ttl_seconds=60)
