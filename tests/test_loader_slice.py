"""loader.load_slice + date-index resolution.

Existing tests in test_loader.py cover get_store + get_lod_grids. These cover
the L2 cache interaction and the multi-timestamp warning path.
"""

import threading
import time

import numpy as np
import pandas as pd
import pytest
import xarray as xr

import app.services.store.registry as store_registry_module
import app.services.store.slice_loader as loader
from app.services.store.registry import store_registry


@pytest.fixture(autouse=True)
def isolate_caches():
    """Clear the store registry before/after each test."""
    store_registry.clear()
    yield
    store_registry.clear()


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


# --- concurrent stampede protection (always in-process, independent of CACHE_BACKEND) ---


def test_concurrent_identical_loads_share_one_compute(monkeypatch):
    """Even under CACHE_BACKEND=none (no cache backend), concurrent identical
    load_slice calls must share one _compute_slice_from_store, not each redo the
    S3 fetch independently. This is what `_slice_dedup` (services.caching.deduper)
    protects — see its docstring for why this matters even without a cache."""
    ds = _ds_with_time(["2024-01-15T13:00:00"])
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)

    calls = 0
    proceed = threading.Event()
    real_compute = loader._compute_slice_from_store

    def slow_compute(*args, **kwargs):
        nonlocal calls
        calls += 1
        proceed.wait(timeout=2)
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(loader, "_compute_slice_from_store", slow_compute)

    results: list = []

    def worker():
        results.append(loader.load_slice("s3://b/x.zarr", "2024-01-16", ["v"]))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.1)  # let all threads register on the in-flight key
    proceed.set()
    for t in threads:
        t.join(timeout=2)

    assert calls == 1, "expected exactly one compute; the rest should share it via _slice_dedup"
    assert len(results) == 4
