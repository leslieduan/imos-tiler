"""GET /admin/cache and DELETE /admin/cache/memory: response shape, auth, in-flight attribution."""

import pytest
from starlette.testclient import TestClient

import app.services.caching.processed_cache as processed_cache
import app.services.caching.slice_cache as loader
from app.main import app
from app.services.product.product import Product
from app.services.product.registry import PRODUCTS

client = TestClient(app, raise_server_exceptions=True)

_ADMIN_KEY = "test-secret"
_HEADERS = {"X-Admin-Key": _ADMIN_KEY}


@pytest.fixture(autouse=True)
def admin_key_env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", _ADMIN_KEY)


@pytest.fixture(autouse=True)
def reset_memoizer_counters():
    """Other tests share the module-level memoizers; reset counters for isolation."""
    loader._slice_memo._peak_inflight = 0
    loader._slice_memo._total_computes = 0
    processed_cache.processed_memo._peak_inflight = 0
    processed_cache.processed_memo._total_computes = 0
    yield


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_get_cache_without_admin_key_returns_401():
    r = client.get("/admin/cache")
    assert r.status_code == 401


def test_get_cache_wrong_admin_key_returns_403():
    r = client.get("/admin/cache", headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def test_get_cache_returns_top_level_keys():
    r = client.get("/admin/cache", headers=_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"in_flight", "memory_cache", "products"}


def test_in_flight_zero_when_idle():
    body = client.get("/admin/cache", headers=_HEADERS).json()
    assert body["in_flight"]["slice"]["current"] == 0
    assert body["in_flight"]["processed"]["current"] == 0


def test_products_in_response_match_registered_products():
    body = client.get("/admin/cache", headers=_HEADERS).json()
    # The conftest seeds these two products.
    assert set(body["products"].keys()) == {"sea_level_anomaly", "ocean_current"}
    for entry in body["products"].values():
        assert set(entry.keys()) == {"slice_in_flight", "processed_in_flight"}


# ---------------------------------------------------------------------------
# In-flight attribution
# ---------------------------------------------------------------------------


def test_inflight_breakdown_distinguishes_products_sharing_a_store():
    """Two products on the same source_path differ only by variables — must not collide."""
    sla = PRODUCTS["sea_level_anomaly"]  # GSLA, single var
    cur = PRODUCTS["ocean_current"]  # UCUR + VCUR, multi var

    # Simulate one in-flight slice per product. We don't need real Futures —
    # the response only inspects keys, not values.
    sla_key = (sla.source_path, "2024-01-01", tuple(sorted(sla.variables)))
    cur_key = (cur.source_path, "2024-01-01", tuple(sorted(cur.variables)))

    with loader._slice_memo._lock:
        loader._slice_memo._inflight[sla_key] = object()  # type: ignore[assignment]
        loader._slice_memo._inflight[cur_key] = object()  # type: ignore[assignment]

    try:
        body = client.get("/admin/cache", headers=_HEADERS).json()
        assert body["products"]["sea_level_anomaly"]["slice_in_flight"] == 1
        assert body["products"]["ocean_current"]["slice_in_flight"] == 1
        assert body["in_flight"]["slice"]["current"] == 2
    finally:
        with loader._slice_memo._lock:
            loader._slice_memo._inflight.pop(sla_key, None)
            loader._slice_memo._inflight.pop(cur_key, None)


def test_inflight_unknown_store_not_attributed_to_any_product():
    """An in-flight key for a deregistered/unknown store should be counted globally
    but not assigned to any product."""
    ghost_key = ("s3://gone/unknown.zarr", "2024-01-01", ("v",))
    with loader._slice_memo._lock:
        loader._slice_memo._inflight[ghost_key] = object()  # type: ignore[assignment]
    try:
        body = client.get("/admin/cache", headers=_HEADERS).json()
        assert body["in_flight"]["slice"]["current"] == 1
        assert all(entry["slice_in_flight"] == 0 for entry in body["products"].values())
    finally:
        with loader._slice_memo._lock:
            loader._slice_memo._inflight.pop(ghost_key, None)


def test_unknown_product_creating_unrelated_inflight_key_not_attributed():
    # Sanity check that totals still come through with a real test product not in PRODUCTS.
    extra = Product(id="not_registered", source_path="s3://other/z.zarr", variable="X")
    key = (extra.source_path, "2024-01-01", ("X",))
    with loader._slice_memo._lock:
        loader._slice_memo._inflight[key] = object()  # type: ignore[assignment]
    try:
        body = client.get("/admin/cache", headers=_HEADERS).json()
        assert body["in_flight"]["slice"]["current"] == 1
        # No product entry for it.
        assert "not_registered" not in body["products"]
    finally:
        with loader._slice_memo._lock:
            loader._slice_memo._inflight.pop(key, None)
