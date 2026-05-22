"""GET /admin/cache and DELETE /admin/cache/{memory,disk}: response shape, auth, disk stats, refresh status, in-flight attribution."""

from unittest.mock import patch

import anyio
import pytest
from starlette.testclient import TestClient

import app.services.data_renderer as data_renderer
import app.services.disk_cache as disk_cache
import app.services.loader as loader
from app.constants import PRODUCTS, Product
from app.main import app

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
    data_renderer._processed_memo._peak_inflight = 0
    data_renderer._processed_memo._total_computes = 0
    yield


@pytest.fixture(autouse=True)
def reset_refresh_status():
    """Refresh status is module-level; reset to the 'never_run' baseline per test."""
    with disk_cache._refresh_status_lock:
        disk_cache._refresh_status.update(
            {
                "last_started_at": None,
                "last_completed_at": None,
                "status": "never_run",
                "last_error": None,
            }
        )
    yield


@pytest.fixture(autouse=True)
def reset_prewarm_running():
    """Prewarm flag is module-level; ensure it is False before and after each test."""
    with disk_cache._prewarm_lock:
        disk_cache._prewarm_running = False
    yield
    with disk_cache._prewarm_lock:
        disk_cache._prewarm_running = False


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", str(tmp_path))
    yield tmp_path


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


def test_get_cache_returns_top_level_keys(monkeypatch):
    r = client.get("/admin/cache", headers=_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"disk", "disk_writes", "in_flight", "memory_cache", "products"}


def test_disk_writes_shape():
    body = client.get("/admin/cache", headers=_HEADERS).json()
    dw = body["disk_writes"]
    assert set(dw.keys()) == {"prewarm", "refresh"}
    assert set(dw["prewarm"].keys()) == {"running"}
    assert set(dw["refresh"].keys()) == {
        "status",
        "last_started_at",
        "last_completed_at",
        "last_error",
        "interval_seconds",
    }


def test_prewarm_running_false_by_default():
    body = client.get("/admin/cache", headers=_HEADERS).json()
    assert body["disk_writes"]["prewarm"]["running"] is False


def test_prewarm_running_true_when_active():
    with disk_cache._prewarm_lock:
        disk_cache._prewarm_running = True
    body = client.get("/admin/cache", headers=_HEADERS).json()
    assert body["disk_writes"]["prewarm"]["running"] is True


def test_refresh_status_never_run_by_default():
    body = client.get("/admin/cache", headers=_HEADERS).json()
    assert body["disk_writes"]["refresh"]["status"] == "never_run"
    assert body["disk_writes"]["refresh"]["last_started_at"] is None
    assert body["disk_writes"]["refresh"]["last_completed_at"] is None
    assert body["disk_writes"]["refresh"]["last_error"] is None


def test_in_flight_zero_when_idle():
    body = client.get("/admin/cache", headers=_HEADERS).json()
    assert body["in_flight"]["slice"]["current"] == 0
    assert body["in_flight"]["processed"]["current"] == 0


def test_products_in_response_match_registered_products():
    body = client.get("/admin/cache", headers=_HEADERS).json()
    # The conftest seeds these two products.
    assert set(body["products"].keys()) == {"sea_level_anomaly", "ocean_current"}
    for entry in body["products"].values():
        assert set(entry.keys()) == {
            "file_count",
            "total_bytes",
            "oldest_date",
            "newest_date",
            "last_write_at",
            "files",
            "cache_dir",
            "slice_in_flight",
            "processed_in_flight",
        }


# ---------------------------------------------------------------------------
# Per-product disk stats
# ---------------------------------------------------------------------------


def test_per_product_disk_stats_reflect_files_on_disk(cache_root):
    """File count, byte total, and date range should mirror what's actually on disk."""
    sla = PRODUCTS["sea_level_anomaly"]
    cache_dir = disk_cache.disk_cache_path(sla.source_path, "x", sla.variables).parent
    cache_dir.mkdir(parents=True)
    (cache_dir / "2024-01-01.pkl.lz4").write_bytes(b"x" * 100)
    (cache_dir / "2024-06-15.pkl.lz4").write_bytes(b"x" * 250)

    body = client.get("/admin/cache", headers=_HEADERS).json()
    sla = body["products"]["sea_level_anomaly"]

    assert sla["file_count"] == 2
    assert sla["total_bytes"] == 350
    assert sla["oldest_date"] == "2024-01-01"
    assert sla["newest_date"] == "2024-06-15"
    assert sla["last_write_at"] is not None  # ISO UTC timestamp


def test_per_product_disk_stats_empty_when_no_files(cache_root):
    body = client.get("/admin/cache", headers=_HEADERS).json()
    sla = body["products"]["sea_level_anomaly"]
    assert sla["file_count"] == 0
    assert sla["total_bytes"] == 0
    assert sla["oldest_date"] is None
    assert sla["newest_date"] is None
    assert sla["last_write_at"] is None


def test_global_disk_stats_aggregate_across_products(cache_root, monkeypatch):
    monkeypatch.setenv("DISK_CACHE_LIMIT_GB", "1")
    monkeypatch.setenv("DISK_EVICTION_THRESHOLD", "0.85")

    sla = PRODUCTS["sea_level_anomaly"]
    cache_dir = disk_cache.disk_cache_path(sla.source_path, "x", sla.variables).parent
    cache_dir.mkdir(parents=True)
    (cache_dir / "2024-01-01.pkl.lz4").write_bytes(b"x" * 1000)

    body = client.get("/admin/cache", headers=_HEADERS).json()
    assert body["disk"]["total_bytes"] == 1000
    assert body["disk"]["limit_bytes"] == 1024**3
    assert body["disk"]["over_eviction_threshold"] is False


# ---------------------------------------------------------------------------
# Refresh status after a run
# ---------------------------------------------------------------------------


def test_refresh_status_ok_after_successful_run(cache_root):
    p = PRODUCTS["sea_level_anomaly"]
    with (
        patch("app.services.loader.get_available_dates", return_value=[]),
        patch("app.services.loader.load_slice"),
    ):
        anyio.run(disk_cache.refresh_disk_cache, [p])

    body = client.get("/admin/cache", headers=_HEADERS).json()
    assert body["disk_writes"]["refresh"]["status"] == "ok"
    assert body["disk_writes"]["refresh"]["last_started_at"] is not None
    assert body["disk_writes"]["refresh"]["last_completed_at"] is not None
    assert body["disk_writes"]["refresh"]["last_error"] is None


def test_refresh_status_error_when_run_raises(cache_root):
    p = PRODUCTS["sea_level_anomaly"]
    # evict_stale_and_orphans is the first sub-call inside refresh; force it to blow up.
    with (
        patch.object(disk_cache, "evict_stale_and_orphans", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        anyio.run(disk_cache.refresh_disk_cache, [p])

    body = client.get("/admin/cache", headers=_HEADERS).json()
    assert body["disk_writes"]["refresh"]["status"] == "error"
    assert "boom" in body["disk_writes"]["refresh"]["last_error"]


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


# ---------------------------------------------------------------------------
# DELETE /admin/cache/disk — auth
# ---------------------------------------------------------------------------


def test_delete_disk_cache_without_admin_key_returns_401():
    r = client.delete("/admin/cache/disk")
    assert r.status_code == 401


def test_delete_disk_cache_wrong_admin_key_returns_403():
    r = client.delete("/admin/cache/disk", headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /admin/cache/disk — 409 guards
# ---------------------------------------------------------------------------


def test_delete_disk_cache_returns_409_when_prewarm_running(monkeypatch):
    with disk_cache._prewarm_lock:
        disk_cache._prewarm_running = True
    r = client.delete("/admin/cache/disk", headers=_HEADERS)
    assert r.status_code == 409
    assert "prewarm" in r.json()["detail"].lower()


def test_delete_disk_cache_returns_409_when_refresh_running():
    with disk_cache._refresh_status_lock:
        disk_cache._refresh_status["status"] = "running"
    r = client.delete("/admin/cache/disk", headers=_HEADERS)
    assert r.status_code == 409
    assert "refresh" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# DELETE /admin/cache/disk — behaviour
# ---------------------------------------------------------------------------


def test_delete_disk_cache_removes_files_and_dirs(cache_root):
    p = PRODUCTS["sea_level_anomaly"]
    d = disk_cache.disk_cache_path(p.source_path, "x", p.variables).parent
    d.mkdir(parents=True)
    (d / "2024-01-01.pkl.lz4").write_bytes(b"x")
    (d / "2024-01-02.pkl.lz4").write_bytes(b"x")

    r = client.delete("/admin/cache/disk", headers=_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body == {"files": 2, "directories": 1}
    assert not d.exists()
    assert cache_root.exists()  # base dir preserved


def test_delete_disk_cache_empty_cache_returns_zeros(cache_root):
    r = client.delete("/admin/cache/disk", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"files": 0, "directories": 0}
