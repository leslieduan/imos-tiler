"""loader.load_slice + evict_product_cache + date-index resolution.

Existing tests in test_loader.py cover get_store + get_lod_grids. These cover
the L2 cache interaction, the multi-timestamp warning path, and the fan-out
eviction across L1/L2.
"""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

import app.services.caching.lifecycle as lifecycle
import app.services.caching.slice_cache as loader
import app.services.store.registry as store_registry_module
from app.services.product.product import Product
from app.services.store.registry import store_registry


@pytest.fixture(autouse=True)
def isolate_caches():
    """Clear in-memory caches before/after each test."""
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


# --- ocean_masked flag ---

# Geographic cells relative to the committed ocean mask (lon 50–190°E, lat −60–10°):
# (-40, 150) is open Southern Ocean (valid); (-6.4, 137) is over New Guinea (masked).


def _ds_ocean(times: list[str], lats: list[float], lons: list[float]) -> xr.Dataset:
    t = pd.to_datetime(times)
    data = np.ones((len(times), len(lats), len(lons)), dtype=np.float32)
    return xr.Dataset(
        {
            "v": xr.DataArray(
                data, dims=["time", "lat", "lon"], coords={"time": t, "lat": lats, "lon": lons}
            )
        }
    )


def test_load_slice_ocean_masked_nulls_invalid_cells(monkeypatch):
    ds = _ds_ocean(["2024-01-15T13:00:00"], [-40.0, -6.4], [150.0, 137.0])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    result = loader.load_slice("s3://b/x.zarr", "2024-01-16", ["v"], ocean_masked=True)
    # Open-ocean cell survives; the New Guinea land cell is nulled.
    assert float(result["v"].sel(lat=-40.0, lon=150.0)) == 1.0
    assert np.isnan(float(result["v"].sel(lat=-6.4, lon=137.0)))


def test_load_slice_without_ocean_masked_keeps_all_cells(monkeypatch):
    ds = _ds_ocean(["2024-01-15T13:00:00"], [-40.0, -6.4], [150.0, 137.0])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    result = loader.load_slice("s3://b/x.zarr", "2024-01-16", ["v"])  # flag defaults off
    assert not np.isnan(result["v"]).any()


def test_evict_product_cache_clears_l2(monkeypatch):
    """evict_product_cache must clear slice cache entries for the product."""
    p = Product(id="ev", source_path="s3://b/ev.zarr", variable="v")
    ds = _ds_with_time(["2024-01-15T13:00:00"])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    # Populate L2.
    loader.load_slice(p.source_path, "2024-01-16", ["v"])
    assert any(k[0] == p.source_path for k in loader._slice_cache)

    lifecycle.evict_product_cache(p)

    # L2 entries for this product must be gone.
    assert not any(k[0] == p.source_path for k in loader._slice_cache)


def test_evict_product_cache_leaves_other_products_alone(monkeypatch):
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
