"""Lifespan tests for main.py.

The hot landmine: sync work inside background tasks MUST be offloaded
(``anyio.to_thread.run_sync``) or the event loop freezes. After the
asyncio→anyio refactor, ``prewarm_disk_slices`` and ``refresh_disk_cache``
are themselves async and offload internally; only ``evict_stale_and_orphans``
remains sync and must be wrapped at the call site.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import app.main as main


@pytest.mark.anyio
async def test_startup_cache_sync_offloads_evict_and_awaits_prewarm():
    """evict_stale_and_orphans is sync → must go through anyio.to_thread.run_sync.
    prewarm_disk_slices is async → must be awaited directly."""
    threaded: list = []

    async def fake_run_sync(fn, *args, **kwargs):
        threaded.append(fn)
        return fn(*args, **kwargs)

    with (
        patch("app.main.evict_stale_and_orphans") as evict,
        patch("app.main.prewarm_disk_slices", new_callable=AsyncMock) as prewarm,
        patch("app.main.anyio.to_thread.run_sync", side_effect=fake_run_sync),
    ):
        await main._startup_cache_sync([])

    assert evict in threaded, "evict_stale_and_orphans was called outside anyio.to_thread.run_sync"
    evict.assert_called_once_with([])
    prewarm.assert_awaited_once_with([])


@pytest.mark.anyio
async def test_startup_cache_sync_continues_after_eviction_failure():
    """An eviction crash must not abort the prewarm — the log-and-continue path."""

    async def passthrough(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("app.main.evict_stale_and_orphans", side_effect=RuntimeError("boom")),
        patch("app.main.prewarm_disk_slices", new_callable=AsyncMock) as prewarm,
        patch("app.main.anyio.to_thread.run_sync", side_effect=passthrough),
    ):
        # Should NOT raise — the function swallows the eviction failure.
        await main._startup_cache_sync([])
    prewarm.assert_awaited_once()


@pytest.mark.anyio
async def test_cache_refresh_loop_awaits_refresh():
    """refresh_disk_cache is async and must be awaited each cycle."""
    iterations = 0

    async def fake_sleep(_seconds):
        # First tick: returns and lets the body run. Second tick: cancel.
        nonlocal iterations
        iterations += 1
        if iterations >= 2:
            raise asyncio.CancelledError

    with (
        patch("app.main.refresh_disk_cache", new_callable=AsyncMock) as refresh,
        patch("app.main.asyncio.sleep", side_effect=fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await main._cache_refresh_loop(interval=1)

    refresh.assert_awaited_once()  # one full body before the second sleep cancelled


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

    async def flaky_refresh(_products):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first cycle fails")

    with (
        patch("app.main.refresh_disk_cache", side_effect=flaky_refresh),
        patch("app.main.asyncio.sleep", side_effect=fake_sleep),
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
