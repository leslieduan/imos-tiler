"""loader.load_slice + evict_product_cache + date-index resolution.

Existing tests in test_loader.py cover get_store + get_lod_grids. These cover
the L2/L3 cache interaction, the multi-timestamp warning path, and the
fan-out eviction across L1/L2/L3.
"""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from constants import Product
from services import disk_cache, loader
from services import store_registry as store_registry_module
from services.store_registry import store_registry


@pytest.fixture(autouse=True)
def isolate_caches(monkeypatch, tmp_path):
    """Clear in-memory caches and isolate disk cache to tmp before/after each test."""
    monkeypatch.setenv("DISK_CACHE_PATH", str(tmp_path))
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
    """Two UTC timestamps mapping to the same local date should warn but still serve."""
    # Two times that both land on Sydney local date 2024-01-16.
    ds = _ds_with_time(["2024-01-15T13:00:00", "2024-01-15T14:00:00"])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    # Warning now fires from store_registry when building the date index (once per
    # store open), not from loader on every load. Patch that logger directly.
    warnings: list = []
    monkeypatch.setattr(
        store_registry_module.logger, "warning", lambda *a, **kw: warnings.append(a)
    )

    loader.load_slice("s3://b/x.zarr", "2024-01-16", ["v"])

    assert warnings, "expected a warning when multiple UTC times map to one local date"
    assert any("Multiple timestamps" in str(args[0]) for args in warnings)


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

    loader.evict_product_cache(p)

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

    loader.evict_product_cache(p_drop)

    # keep's entry still in slice cache.
    assert any(k[0] == p_keep.source_path for k in loader._slice_cache)
    assert not any(k[0] == p_drop.source_path for k in loader._slice_cache)
