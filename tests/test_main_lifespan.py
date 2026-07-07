"""Lifespan tests for main.py."""

import pytest

import app.main as main


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
