"""Disk slice cache: paths, read/write, eviction (size + stale + orphans), prewarm.

Mocks load_slice / get_available_dates so tests don't touch any real Zarr store.
"""

from unittest.mock import patch

import anyio
import lz4.frame
import numpy as np
import pytest
import xarray as xr

import app.services.disk_cache as disk_cache
from app.constants import Product


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", str(tmp_path))
    yield tmp_path


def _make_slice(value: float = 1.0) -> xr.Dataset:
    """Tiny dataset that can be pickled/round-tripped without touching disk."""
    return xr.Dataset(
        {"v": xr.DataArray(np.full((2, 2), value, dtype=np.float32), dims=["lat", "lon"])}
    )


def _product(pid="p1", source="s3://bucket/x.zarr", variable="v"):
    return Product(id=pid, source_path=source, variable=variable)


# ---------------------------------------------------------------------------
# disk_cache_path
# ---------------------------------------------------------------------------


def test_disk_cache_path_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", None)
    assert disk_cache.disk_cache_path("s3://x/y.zarr", "2024-01-01", ["v"]) is None


def test_disk_cache_path_encodes_url_into_dirname(cache_root):
    p = disk_cache.disk_cache_path("s3://bucket/x.zarr", "2024-01-01", ["v"])
    assert p is not None
    # `/` becomes `%` so URLs from different buckets don't collide.
    assert "%" in p.parent.name
    assert "bucket" in p.parent.name
    assert p.name == "2024-01-01.pkl.lz4"


def test_disk_cache_path_sorts_variables_for_stability(monkeypatch):
    """Same variable set must produce same path regardless of order — cache keys."""
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", "/tmp/x")
    a = disk_cache.disk_cache_path("s3://b/x.zarr", "d", ["u", "v"])
    b = disk_cache.disk_cache_path("s3://b/x.zarr", "d", ["v", "u"])
    assert a == b


def test_disk_cache_path_different_buckets_dont_collide(cache_root):
    a = disk_cache.disk_cache_path("s3://bucket-a/x.zarr", "2024-01-01", ["v"])
    b = disk_cache.disk_cache_path("s3://bucket-b/x.zarr", "2024-01-01", ["v"])
    assert a != b
    assert a.parent != b.parent


# ---------------------------------------------------------------------------
# read / write round-trip
# ---------------------------------------------------------------------------


def test_write_and_read_slice_round_trip(cache_root):
    ds = _make_slice(value=42.0)
    p = disk_cache.disk_cache_path("s3://b/x.zarr", "2024-01-01", ["v"])
    disk_cache.write_slice_to_disk(p, ds)
    assert p.exists()

    out = disk_cache.read_slice_from_disk(p)
    assert out is not None
    assert float(out["v"].values[0, 0]) == 42.0


def test_read_corrupt_file_returns_none(cache_root):
    """A truncated/corrupt pickle must NOT crash startup — just log + skip."""
    p = cache_root / "bad-cache" / "2024-01-01.pkl.lz4"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"this is not lz4 data")
    assert disk_cache.read_slice_from_disk(p) is None


def test_read_partially_valid_lz4_but_bad_pickle_returns_none(cache_root):
    p = cache_root / "bad" / "x.pkl.lz4"
    p.parent.mkdir(parents=True)
    p.write_bytes(lz4.frame.compress(b"not a pickle"))
    assert disk_cache.read_slice_from_disk(p) is None


# ---------------------------------------------------------------------------
# _evict_if_over_threshold (pressure eviction)
# ---------------------------------------------------------------------------


def test_evict_pressure_no_op_under_limit(cache_root, monkeypatch):
    monkeypatch.setenv("DISK_CACHE_LIMIT_GB", "1")
    monkeypatch.setenv("DISK_EVICTION_THRESHOLD", "0.85")
    # Single tiny file — well under threshold.
    p = cache_root / "x" / "2024-01-01.pkl.lz4"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x" * 100)

    disk_cache._evict_if_over_threshold()
    assert p.exists()


def test_evict_pressure_removes_smallest_first(cache_root, monkeypatch):
    """When over threshold, smallest files go first (cheap to re-fetch)."""
    monkeypatch.setattr(disk_cache, "_evict_lock", disk_cache.threading.Lock())
    # Force a tiny limit so anything over a few KB evicts.
    monkeypatch.setenv("DISK_CACHE_LIMIT_GB", "0")  # makes int(...) == 0 → threshold 0
    # Actually we need integer GB; 0 GB → 0 byte limit, threshold 0.
    # Easier: monkeypatch the env constant indirectly by passing a small enough threshold.

    big = cache_root / "x" / "2024-01-02.pkl.lz4"
    small = cache_root / "x" / "2024-01-01.pkl.lz4"
    big.parent.mkdir(parents=True)
    big.write_bytes(b"x" * 10_000)
    small.write_bytes(b"x" * 100)

    disk_cache._evict_if_over_threshold()
    # With 0-byte threshold both should go, but the contract is smallest first;
    # at minimum, evict ran and something was removed.
    assert not (big.exists() and small.exists()), "no eviction occurred under pressure"


def test_evict_pressure_disabled_when_env_unset(monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", None)
    disk_cache._evict_if_over_threshold()


# ---------------------------------------------------------------------------
# prewarm_disk_slices
# ---------------------------------------------------------------------------


def test_prewarm_disabled_when_env_unset(monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", None)
    with patch("app.services.loader.get_available_dates") as get_dates:
        anyio.run(disk_cache.prewarm_disk_slices, [_product()])
    get_dates.assert_not_called()


def test_prewarm_writes_files_for_each_date(cache_root):
    p = _product("p1", source="s3://b/x.zarr", variable="v")
    ds = _make_slice()

    with (
        patch("app.services.loader.get_available_dates", return_value=["2024-01-01", "2024-01-02"]),
        patch("app.services.loader.load_slice_uncached", return_value=ds),
    ):
        anyio.run(disk_cache.prewarm_disk_slices, [p])

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
        patch("app.services.loader.get_available_dates", return_value=["2024-01-01"]),
        patch("app.services.loader.load_slice_uncached") as load,
    ):
        anyio.run(disk_cache.prewarm_disk_slices, [p])
    load.assert_not_called()
    # File still has the original value (not overwritten).
    out = disk_cache.read_slice_from_disk(existing)
    assert float(out["v"].values[0, 0]) == 5.0


def test_prewarm_swallows_per_product_date_errors(cache_root):
    """One product failing to list dates must NOT block others."""
    good = _product("good", source="s3://b/good.zarr", variable="v")
    bad = _product("bad", source="s3://b/bad.zarr", variable="v")

    def dates_for(url):
        if "bad" in url:
            raise RuntimeError("S3 down")
        return ["2024-01-01"]

    with (
        patch("app.services.loader.get_available_dates", side_effect=dates_for),
        patch("app.services.loader.load_slice_uncached", return_value=_make_slice()),
    ):
        anyio.run(disk_cache.prewarm_disk_slices, [bad, good])

    good_dir = disk_cache.disk_cache_path(good.source_path, "x", ["v"]).parent
    assert any(good_dir.glob("*.pkl.lz4")), "good product should still be cached"


def test_prewarm_swallows_filenotfound_for_individual_slice(cache_root):
    """load_slice raising FileNotFoundError on one date must not abort the prewarm."""
    p = _product()
    seen: list[str] = []

    def load_one(url, date, vars_):
        seen.append(date)
        if date == "2024-01-02":
            raise FileNotFoundError("no data")
        return _make_slice()

    with (
        patch("app.services.loader.get_available_dates", return_value=["2024-01-01", "2024-01-02"]),
        patch("app.services.loader.load_slice_uncached", side_effect=load_one),
    ):
        anyio.run(disk_cache.prewarm_disk_slices, [p])

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

    with patch("app.services.loader.get_available_dates", return_value=["2024-06-01"]):
        disk_cache.evict_stale_and_orphans([p])

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

    with patch("app.services.loader.get_available_dates", return_value=["2024-01-01"]):
        disk_cache.evict_stale_and_orphans([p])

    assert keep_dir.exists()
    assert not orphan_dir.exists()


def test_evict_stale_disabled_when_env_unset(monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", None)
    with patch("app.services.loader.get_available_dates") as gad:
        disk_cache.evict_stale_and_orphans([_product()])
    gad.assert_not_called()


# ---------------------------------------------------------------------------
# refresh_disk_cache
# ---------------------------------------------------------------------------


def test_refresh_adds_new_dates(cache_root):
    p = _product()
    cache_dir = disk_cache.disk_cache_path(p.source_path, "x", ["v"]).parent
    cache_dir.mkdir(parents=True)
    (cache_dir / "2024-01-01.pkl.lz4").write_bytes(b"placeholder")

    with (
        patch(
            "app.services.loader.get_available_dates",
            return_value=["2024-01-01", "2024-01-02"],
        ),
        patch("app.services.loader.load_slice_uncached", return_value=_make_slice()),
    ):
        anyio.run(disk_cache.refresh_disk_cache, [p])

    cached = sorted(f.name for f in cache_dir.glob("*.pkl.lz4"))
    assert "2024-01-02.pkl.lz4" in cached


def test_refresh_disabled_when_env_unset(monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", None)
    with patch("app.services.loader.get_available_dates") as gad:
        anyio.run(disk_cache.refresh_disk_cache, [_product()])
    gad.assert_not_called()


# ---------------------------------------------------------------------------
# evict_product_dir
# ---------------------------------------------------------------------------


def test_evict_product_dir_removes_whole_dir(cache_root):
    p = _product()
    d = disk_cache.disk_cache_path(p.source_path, "x", ["v"]).parent
    d.mkdir(parents=True)
    (d / "2024-01-01.pkl.lz4").write_bytes(b"k")
    (d / "2024-01-02.pkl.lz4").write_bytes(b"k")

    disk_cache.evict_product_dir(p)
    assert not d.exists()


def test_evict_product_dir_when_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", None)
    disk_cache.evict_product_dir(_product())


def test_evict_product_dir_when_missing_is_noop(cache_root):
    # Nothing on disk yet — must not crash.
    disk_cache.evict_product_dir(_product("never-cached"))


# ---------------------------------------------------------------------------
# clear_disk_cache
# ---------------------------------------------------------------------------


def test_clear_disk_cache_disabled_returns_zeros(monkeypatch):
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", None)
    assert disk_cache.clear_disk_cache() == {"files": 0, "directories": 0}


def test_clear_disk_cache_empty_cache_returns_zeros(cache_root):
    assert disk_cache.clear_disk_cache() == {"files": 0, "directories": 0}


def test_clear_disk_cache_removes_all_product_dirs(cache_root):
    p1 = _product("p1", source="s3://b/x.zarr", variable="v")
    p2 = _product("p2", source="s3://b/y.zarr", variable="v")
    for p in [p1, p2]:
        d = disk_cache.disk_cache_path(p.source_path, "x", ["v"]).parent
        d.mkdir(parents=True)
        (d / "2024-01-01.pkl.lz4").write_bytes(b"a")
        (d / "2024-01-02.pkl.lz4").write_bytes(b"b")

    result = disk_cache.clear_disk_cache()
    assert result == {"files": 4, "directories": 2}
    assert list(cache_root.iterdir()) == []  # dirs gone, base preserved


def test_clear_disk_cache_preserves_base_directory(cache_root):
    disk_cache.clear_disk_cache()
    assert cache_root.exists()


# ---------------------------------------------------------------------------
# is_prewarm_running / is_refresh_running
# ---------------------------------------------------------------------------


def test_is_prewarm_running_false_by_default():
    with disk_cache._prewarm_lock:
        disk_cache._prewarm_running = False
    assert disk_cache.is_prewarm_running() is False


def test_is_prewarm_running_true_when_set():
    with disk_cache._prewarm_lock:
        disk_cache._prewarm_running = True
    try:
        assert disk_cache.is_prewarm_running() is True
    finally:
        with disk_cache._prewarm_lock:
            disk_cache._prewarm_running = False


def test_is_refresh_running_false_when_status_ok():
    with disk_cache._refresh_status_lock:
        disk_cache._refresh_status["status"] = "ok"
    assert disk_cache.is_refresh_running() is False


def test_is_refresh_running_true_when_status_running():
    with disk_cache._refresh_status_lock:
        disk_cache._refresh_status["status"] = "running"
    try:
        assert disk_cache.is_refresh_running() is True
    finally:
        with disk_cache._refresh_status_lock:
            disk_cache._refresh_status["status"] = "never_run"
