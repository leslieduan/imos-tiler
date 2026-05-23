"""Cross-layer cache lifecycle: prewarm, refresh, stale eviction, and the
status accessors that back /admin/cache.

Mocks ``loader.load_slice_uncached`` / ``loader.get_available_dates`` so tests
don't touch any real Zarr store.
"""

from unittest.mock import patch

import anyio
import numpy as np
import pytest
import xarray as xr

import app.services.caching.lifecycle as lifecycle
import app.services.disk_cache as disk_cache
from app.constants import PRODUCTS
from app.domain.product import Product


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", str(tmp_path))
    monkeypatch.setattr(lifecycle, "DISK_CACHE_PATH", str(tmp_path))
    yield tmp_path


def _make_slice(value: float = 1.0) -> xr.Dataset:
    return xr.Dataset(
        {"v": xr.DataArray(np.full((2, 2), value, dtype=np.float32), dims=["lat", "lon"])}
    )


def _product(pid="p1", source="s3://bucket/x.zarr", variable="v"):
    return Product(id=pid, source_path=source, variable=variable)


# ---------------------------------------------------------------------------
# prewarm_disk_slices
# ---------------------------------------------------------------------------


def test_prewarm_writes_files_for_each_date(cache_root, monkeypatch):
    p = _product("p1", source="s3://b/x.zarr", variable="v")
    monkeypatch.setitem(PRODUCTS, p.id, p)  # race-guard in _prewarm_one consults this
    ds = _make_slice()

    with (
        patch(
            "app.services.caching.lifecycle.get_available_dates",
            return_value=["2024-01-01", "2024-01-02"],
        ),
        patch("app.services.caching.lifecycle.load_slice_uncached", return_value=ds),
    ):
        anyio.run(lifecycle.prewarm_disk_slices, [p])

    cache_dir = disk_cache.disk_cache_path(p.source_path, "x", ["v"]).parent
    files = sorted(f.name for f in cache_dir.glob("*.pkl.lz4"))
    assert files == ["2024-01-01.pkl.lz4", "2024-01-02.pkl.lz4"]


def test_prewarm_skips_existing_files(cache_root):
    """Already-cached date should not trigger a redundant S3 fetch."""
    p = _product()
    existing = disk_cache.disk_cache_path(p.source_path, "2024-01-01", ["v"])
    existing.parent.mkdir(parents=True)
    disk_cache.write_slice_to_disk(existing, _make_slice(value=5.0))

    with (
        patch("app.services.caching.lifecycle.get_available_dates", return_value=["2024-01-01"]),
        patch("app.services.caching.lifecycle.load_slice_uncached") as load,
    ):
        anyio.run(lifecycle.prewarm_disk_slices, [p])
    load.assert_not_called()
    # File still has the original value (not overwritten).
    out = disk_cache.read_slice_from_disk(existing)
    assert float(out["v"].values[0, 0]) == 5.0


def test_prewarm_swallows_per_product_date_errors(cache_root, monkeypatch):
    """One product failing to list dates must NOT block others."""
    good = _product("good", source="s3://b/good.zarr", variable="v")
    bad = _product("bad", source="s3://b/bad.zarr", variable="v")
    monkeypatch.setitem(PRODUCTS, good.id, good)
    monkeypatch.setitem(PRODUCTS, bad.id, bad)

    def dates_for(url):
        if "bad" in url:
            raise RuntimeError("S3 down")
        return ["2024-01-01"]

    with (
        patch("app.services.caching.lifecycle.get_available_dates", side_effect=dates_for),
        patch("app.services.caching.lifecycle.load_slice_uncached", return_value=_make_slice()),
    ):
        anyio.run(lifecycle.prewarm_disk_slices, [bad, good])

    good_dir = disk_cache.disk_cache_path(good.source_path, "x", ["v"]).parent
    assert any(good_dir.glob("*.pkl.lz4")), "good product should still be cached"


def test_prewarm_swallows_filenotfound_for_individual_slice(cache_root, monkeypatch):
    """load_slice raising FileNotFoundError on one date must not abort the prewarm."""
    p = _product()
    monkeypatch.setitem(PRODUCTS, p.id, p)
    seen: list[str] = []

    def load_one(url, date, vars_):
        seen.append(date)
        if date == "2024-01-02":
            raise FileNotFoundError("no data")
        return _make_slice()

    with (
        patch(
            "app.services.caching.lifecycle.get_available_dates",
            return_value=["2024-01-01", "2024-01-02"],
        ),
        patch("app.services.caching.lifecycle.load_slice_uncached", side_effect=load_one),
    ):
        anyio.run(lifecycle.prewarm_disk_slices, [p])

    # Both dates attempted; 01 cached, 02 not.
    assert "2024-01-01" in seen and "2024-01-02" in seen
    cache_dir = disk_cache.disk_cache_path(p.source_path, "x", ["v"]).parent
    cached = sorted(f.name for f in cache_dir.glob("*.pkl.lz4"))
    assert cached == ["2024-01-01.pkl.lz4"]


# ---------------------------------------------------------------------------
# evict_stale_and_orphans
# ---------------------------------------------------------------------------


def test_evict_stale_removes_dates_outside_window(cache_root):
    p = _product()
    cache_dir = disk_cache.disk_cache_path(p.source_path, "x", ["v"]).parent
    cache_dir.mkdir(parents=True)
    keep = cache_dir / "2024-06-01.pkl.lz4"
    drop = cache_dir / "2020-01-01.pkl.lz4"
    keep.write_bytes(b"k")
    drop.write_bytes(b"d")

    with patch("app.services.caching.lifecycle.get_available_dates", return_value=["2024-06-01"]):
        lifecycle.evict_stale_and_orphans([p])

    assert keep.exists()
    assert not drop.exists()


def test_evict_orphans_drops_unknown_product_dirs(cache_root):
    """A cache dir not in the products list must be removed."""
    p = _product()
    keep_dir = disk_cache.disk_cache_path(p.source_path, "x", ["v"]).parent
    keep_dir.mkdir(parents=True)
    (keep_dir / "2024-01-01.pkl.lz4").write_bytes(b"k")

    orphan_dir = cache_root / "orphan-prod"
    orphan_dir.mkdir()
    (orphan_dir / "2024-01-01.pkl.lz4").write_bytes(b"o")

    with patch("app.services.caching.lifecycle.get_available_dates", return_value=["2024-01-01"]):
        lifecycle.evict_stale_and_orphans([p])

    assert keep_dir.exists()
    assert not orphan_dir.exists()


# ---------------------------------------------------------------------------
# refresh_disk_cache
# ---------------------------------------------------------------------------


def test_refresh_adds_new_dates(cache_root, monkeypatch):
    p = _product()
    monkeypatch.setitem(PRODUCTS, p.id, p)
    cache_dir = disk_cache.disk_cache_path(p.source_path, "x", ["v"]).parent
    cache_dir.mkdir(parents=True)
    (cache_dir / "2024-01-01.pkl.lz4").write_bytes(b"placeholder")

    with (
        patch(
            "app.services.caching.lifecycle.get_available_dates",
            return_value=["2024-01-01", "2024-01-02"],
        ),
        patch("app.services.caching.lifecycle.load_slice_uncached", return_value=_make_slice()),
    ):
        anyio.run(lifecycle.refresh_disk_cache, [p])

    cached = sorted(f.name for f in cache_dir.glob("*.pkl.lz4"))
    assert "2024-01-02.pkl.lz4" in cached


# ---------------------------------------------------------------------------
# is_prewarm_running / is_refresh_running
# ---------------------------------------------------------------------------


def test_is_prewarm_running_false_by_default():
    with lifecycle._prewarm_lock:
        lifecycle._prewarm_running = False
    assert lifecycle.is_prewarm_running() is False


def test_is_prewarm_running_true_when_set():
    with lifecycle._prewarm_lock:
        lifecycle._prewarm_running = True
    try:
        assert lifecycle.is_prewarm_running() is True
    finally:
        with lifecycle._prewarm_lock:
            lifecycle._prewarm_running = False


def test_is_refresh_running_false_when_status_ok():
    with lifecycle._refresh_status_lock:
        lifecycle._refresh_status["status"] = "ok"
    assert lifecycle.is_refresh_running() is False


def test_is_refresh_running_true_when_status_running():
    with lifecycle._refresh_status_lock:
        lifecycle._refresh_status["status"] = "running"
    try:
        assert lifecycle.is_refresh_running() is True
    finally:
        with lifecycle._refresh_status_lock:
            lifecycle._refresh_status["status"] = "never_run"
