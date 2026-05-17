"""Lifespan tests for main.py.

CLAUDE.md flags this as a hot landmine: any CPU/IO-heavy work inside the
background tasks MUST be wrapped in asyncio.to_thread or the event loop
freezes and in-flight requests stall. These tests pin that contract.
"""

import asyncio
from unittest.mock import patch

import pytest

import main


@pytest.mark.anyio
async def test_startup_cache_sync_offloads_via_to_thread():
    """evict_stale_and_orphans and prewarm_disk_slices must be wrapped in to_thread."""
    threaded: list = []

    async def fake_to_thread(fn, *args, **kwargs):
        threaded.append(fn)
        return fn(*args, **kwargs)

    with (
        patch("main.evict_stale_and_orphans") as evict,
        patch("main.prewarm_disk_slices") as prewarm,
        patch("main.asyncio.to_thread", side_effect=fake_to_thread),
    ):
        await main._startup_cache_sync([])

    assert evict in threaded, "evict_stale_and_orphans was called outside asyncio.to_thread"
    assert prewarm in threaded, "prewarm_disk_slices was called outside asyncio.to_thread"
    evict.assert_called_once_with([])
    prewarm.assert_called_once_with([])


@pytest.mark.anyio
async def test_startup_cache_sync_continues_after_eviction_failure():
    """An eviction crash must not abort the prewarm — the log-and-continue path."""

    async def passthrough(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("main.evict_stale_and_orphans", side_effect=RuntimeError("boom")),
        patch("main.prewarm_disk_slices") as prewarm,
        patch("main.asyncio.to_thread", side_effect=passthrough),
    ):
        # Should NOT raise — the function swallows the eviction failure.
        await main._startup_cache_sync([])
    prewarm.assert_called_once()


@pytest.mark.anyio
async def test_cache_refresh_loop_offloads_refresh():
    """refresh_disk_cache must run via asyncio.to_thread, not block the loop."""
    threaded: list = []
    iterations = 0

    async def fake_sleep(_seconds):
        # First tick: returns and lets the body run. Second tick: cancel.
        nonlocal iterations
        iterations += 1
        if iterations >= 2:
            raise asyncio.CancelledError

    async def fake_to_thread(fn, *args, **kwargs):
        threaded.append(fn)
        return fn(*args, **kwargs)

    with (
        patch("main.refresh_disk_cache") as refresh,
        patch("main.asyncio.sleep", side_effect=fake_sleep),
        patch("main.asyncio.to_thread", side_effect=fake_to_thread),
    ):
        with pytest.raises(asyncio.CancelledError):
            await main._cache_refresh_loop(interval=1)

    assert threaded == [refresh], "refresh_disk_cache must be wrapped in asyncio.to_thread"
    assert refresh.call_count == 1  # one full body before the second sleep cancelled


@pytest.mark.anyio
async def test_cache_refresh_loop_survives_refresh_exception():
    """One bad cycle must not kill the loop — caught and the next cycle still runs."""
    iterations = 0

    async def fake_sleep(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations >= 3:
            raise asyncio.CancelledError

    call_count = 0

    def flaky_refresh(_products):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first cycle fails")

    async def passthrough(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("main.refresh_disk_cache", side_effect=flaky_refresh),
        patch("main.asyncio.sleep", side_effect=fake_sleep),
        patch("main.asyncio.to_thread", side_effect=passthrough),
    ):
        with pytest.raises(asyncio.CancelledError):
            await main._cache_refresh_loop(interval=1)

    assert call_count == 2  # second cycle ran after the first one crashed


@pytest.fixture
def anyio_backend():
    """Force anyio tests to run under asyncio (not trio)."""
    return "asyncio"


def test_health_endpoint():
    """Sanity: /health returns 200 — exercises that the app boots."""
    from starlette.testclient import TestClient

    client = TestClient(main.app, raise_server_exceptions=True)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_global_exception_handler_returns_500():
    """An unhandled exception in a request handler must return a 500 JSON body."""
    from fastapi import APIRouter
    from starlette.testclient import TestClient

    test_app = type(main.app)(title="t")
    test_app.add_exception_handler(Exception, main.global_exception_handler)

    router = APIRouter()

    @router.get("/boom")
    def boom():
        raise RuntimeError("explode")

    test_app.include_router(router)
    client = TestClient(test_app, raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal server error"}
