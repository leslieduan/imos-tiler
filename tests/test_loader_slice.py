"""loader.load_slice + evict_product_cache + date-index resolution.

Existing tests in test_loader.py cover get_store + get_lod_grids. These cover
the L2/L3 cache interaction, the multi-timestamp warning path, and the
fan-out eviction across L1/L2/L3.
"""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

import app.services.caching.disk as disk_cache
import app.services.caching.lifecycle as lifecycle
import app.services.caching.slice_cache as loader
import app.services.store.registry as store_registry_module
from app.services.product.product import Product
from app.services.store.registry import store_registry


@pytest.fixture(autouse=True)
def isolate_caches(monkeypatch, tmp_path):
    """Clear in-memory caches and isolate disk cache to tmp before/after each test."""
    monkeypatch.setattr(disk_cache, "DISK_CACHE_PATH", str(tmp_path))
    store_registry.clear()
    loader._slice_cache.clear()
    loader._slice_memo._inflight.clear()
    yield
    store_registry.clear()
    loader._slice_cache.clear()
    loader._slice_memo._inflight.clear()


def _ds_with_time(times: list[str]) -> xr.Dataset:
    """Build a 4D dataset with explicit time coordinates (UTC)."""
    t = pd.to_datetime(times)
    return xr.Dataset(
        {
            "v": xr.DataArray(
                np.arange(len(times) * 4).reshape(len(times), 2, 2).astype(np.float32),
                dims=["time", "lat", "lon"],
                coords={
                    "time": t,
                    "lat": [0.0, 1.0],
                    "lon": [0.0, 1.0],
                },
            )
        }
    )


def test_load_slice_returns_dataset_for_known_date(monkeypatch):
    """Happy path: date in index → slice computed and returned."""
    # 2024-01-15 UTC → local date 2024-01-16 in Sydney (UTC+11).
    ds = _ds_with_time(["2024-01-15T13:00:00"])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    result = loader.load_slice("s3://b/x.zarr", "2024-01-16", ["v"])
    assert "v" in result.data_vars
    assert result["v"].shape == (2, 2)


def test_load_slice_unknown_date_raises_file_not_found(monkeypatch):
    ds = _ds_with_time(["2024-01-15T13:00:00"])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    with pytest.raises(FileNotFoundError, match="Latest available date is '2024-01-16'"):
        loader.load_slice("s3://b/x.zarr", "1999-01-01", ["v"])


def test_load_slice_caches_result(monkeypatch):
    """Second call for the same key must NOT hit the underlying store again."""
    ds = _ds_with_time(["2024-01-15T13:00:00"])
    opens = 0

    def fake_open(*_, **__):
        nonlocal opens
        opens += 1
        return ds

    monkeypatch.setattr(xr, "open_zarr", fake_open)

    loader.load_slice("s3://b/x.zarr", "2024-01-16", ["v"])
    loader.load_slice("s3://b/x.zarr", "2024-01-16", ["v"])
    assert opens == 1, "second call should hit slice cache, not open_zarr again"


def test_load_slice_uses_l3_disk_cache_on_miss(monkeypatch, tmp_path):
    """If the slice exists on disk, load_slice must read it without touching the store."""
    product_source = "s3://b/x.zarr"
    cached = xr.Dataset({"v": xr.DataArray(np.full((2, 2), 99.0), dims=["lat", "lon"])})
    p = disk_cache.disk_cache_path(product_source, "2099-01-01", ["v"])
    p.parent.mkdir(parents=True)
    disk_cache.write_slice_to_disk(p, cached)

    # Should never be called — return value just satisfies the registry probe.
    def fake_open(*_, **__):
        raise AssertionError("open_zarr called even though disk cache had the slice")

    monkeypatch.setattr(xr, "open_zarr", fake_open)

    result = loader.load_slice(product_source, "2099-01-01", ["v"])
    assert float(result["v"].values[0, 0]) == 99.0


def test_load_slice_warns_on_multiple_timestamps_per_date(monkeypatch):
    """Two UTC timestamps mapping to the same local date should log debug but still serve."""
    # Two times that both land on Sydney local date 2024-01-16.
    ds = _ds_with_time(["2024-01-15T13:00:00", "2024-01-15T14:00:00"])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    # Debug log fires from store_registry when building the date index (once per
    # store open), not from loader on every load. Patch that logger directly.
    debug_calls: list = []
    monkeypatch.setattr(
        store_registry_module.logger, "debug", lambda *a, **kw: debug_calls.append(a)
    )

    loader.load_slice("s3://b/x.zarr", "2024-01-16", ["v"])

    assert debug_calls, "expected a debug log when multiple UTC times map to one local date"
    assert any("Multiple timestamps" in str(args[0]) for args in debug_calls)


# --- load_point_series ---

# 2024-01-15/16/17 at 13:00 UTC → Sydney (UTC+11) local dates 2024-01-16/17/18.
_SERIES_TIMES = ["2024-01-15T13:00:00", "2024-01-16T13:00:00", "2024-01-17T13:00:00"]
_SERIES_DATES = ["2024-01-16", "2024-01-17", "2024-01-18"]


def test_load_point_series_returns_series_for_full_range(monkeypatch):
    ds = _ds_with_time(_SERIES_TIMES)
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    # lat=0.4,lon=0.6 → nearest cell (lat=0, lon=1). In _ds_with_time the value at
    # time index t for that cell is 4*t + 1.
    lat0, lon0, dates, point_ds = loader.load_point_series(
        "s3://b/x.zarr", ["v"], 0.4, 0.6, "2024-01-16", "2024-01-18"
    )
    assert (lat0, lon0) == (0.0, 1.0)
    assert dates == _SERIES_DATES
    assert point_ds["v"].sizes["time"] == 3
    assert [float(point_ds["v"].isel(time=i)) for i in range(3)] == [1.0, 5.0, 9.0]


def test_load_point_series_filters_to_subrange(monkeypatch):
    ds = _ds_with_time(_SERIES_TIMES)
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    _, _, dates, point_ds = loader.load_point_series(
        "s3://b/x.zarr", ["v"], 0.0, 0.0, "2024-01-17", "2024-01-17"
    )
    assert dates == ["2024-01-17"]
    assert point_ds["v"].sizes["time"] == 1


def test_load_point_series_unbounded_to(monkeypatch):
    ds = _ds_with_time(_SERIES_TIMES)
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    _, _, dates, _ = loader.load_point_series("s3://b/x.zarr", ["v"], 0.0, 0.0, "2024-01-17", None)
    assert dates == ["2024-01-17", "2024-01-18"]


def test_load_point_series_empty_range_resolves_cell_but_no_data(monkeypatch):
    ds = _ds_with_time(_SERIES_TIMES)
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    lat0, lon0, dates, point_ds = loader.load_point_series(
        "s3://b/x.zarr", ["v"], 0.0, 0.0, "2099-01-01", "2099-12-31"
    )
    assert dates == []
    assert point_ds is None
    assert (lat0, lon0) == (0.0, 0.0)


def test_evict_product_cache_clears_l2_and_disk(monkeypatch, tmp_path):
    """evict_product_cache must clear slice cache entries AND remove the disk dir."""
    p = Product(id="ev", source_path="s3://b/ev.zarr", variable="v")
    ds = _ds_with_time(["2024-01-15T13:00:00"])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    # Populate L2.
    loader.load_slice(p.source_path, "2024-01-16", ["v"])
    assert any(k[0] == p.source_path for k in loader._slice_cache)

    # Populate L3.
    disk_p = disk_cache.disk_cache_path(p.source_path, "2024-01-16", ["v"])
    disk_p.parent.mkdir(parents=True, exist_ok=True)
    disk_cache.write_slice_to_disk(disk_p, ds.isel(time=0))
    assert disk_p.exists()

    lifecycle.evict_product_cache(p)

    # L2 entries for this product must be gone.
    assert not any(k[0] == p.source_path for k in loader._slice_cache)
    # L3 dir must be gone.
    assert not disk_p.parent.exists()


def test_evict_product_cache_leaves_other_products_alone(monkeypatch, tmp_path):
    p_keep = Product(id="keep", source_path="s3://b/keep.zarr", variable="v")
    p_drop = Product(id="drop", source_path="s3://b/drop.zarr", variable="v")
    ds = _ds_with_time(["2024-01-15T13:00:00"])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    loader.load_slice(p_keep.source_path, "2024-01-16", ["v"])
    loader.load_slice(p_drop.source_path, "2024-01-16", ["v"])

    lifecycle.evict_product_cache(p_drop)

    # keep's entry still in slice cache.
    assert any(k[0] == p_keep.source_path for k in loader._slice_cache)
    assert not any(k[0] == p_drop.source_path for k in loader._slice_cache)
