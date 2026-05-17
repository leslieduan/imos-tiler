import numpy as np
import pytest
import xarray as xr

from constants import Product
from services.loader import get_lod_grids
from services.store_registry import _storage_options, get_store, store_registry


def _make_ds(**dims: int) -> xr.Dataset:
    shape = list(dims.values())
    coords = {k: np.arange(v, dtype=float) for k, v in dims.items()}
    return xr.Dataset({"var": xr.DataArray(np.zeros(shape), dims=list(dims.keys()), coords=coords)})


@pytest.fixture(autouse=True)
def clear_stores():
    store_registry.clear()
    yield
    store_registry.clear()


def test_get_store_raises_when_lat_missing(monkeypatch):
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: _make_ds(time=2, lon=10))
    with pytest.raises(ValueError, match="missing lat/lon dims"):
        get_store("s3://test/no_lat.zarr")


def test_get_store_raises_when_lon_missing(monkeypatch):
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: _make_ds(time=2, lat=10))
    with pytest.raises(ValueError, match="missing lat/lon dims"):
        get_store("s3://test/no_lon.zarr")


def test_get_store_normalises_coord_names(monkeypatch):
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: _make_ds(TIME=2, LATITUDE=5, LONGITUDE=8))
    result = get_store("s3://test/uppercase.zarr")
    assert "lat" in result.dims
    assert "lon" in result.dims
    assert "time" in result.dims
    assert "LATITUDE" not in result.dims


def test_get_store_sortby_time(monkeypatch):
    ds = _make_ds(time=4, lat=5, lon=8)
    ds = ds.assign_coords(time=np.array([4.0, 1.0, 3.0, 2.0]))
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: ds)
    result = get_store("s3://test/unsorted.zarr")
    assert list(result.time.values) == sorted(result.time.values)


def test_get_lod_grids_populates_product(monkeypatch):
    monkeypatch.setattr(xr, "open_zarr", lambda *_, **__: _make_ds(time=1, lat=74, lon=102))
    product = Product(id="t1", source_path="s3://test/grids.zarr", variable="var")
    assert product.lod_grids == {}
    grids = get_lod_grids(product)
    assert grids
    assert product.lod_grids is grids


def test_get_lod_grids_fast_path_skips_store(monkeypatch):
    opened = []
    monkeypatch.setattr(
        xr, "open_zarr", lambda *_, **__: opened.append(1) or _make_ds(lat=5, lon=5)
    )
    product = Product(
        id="t2", source_path="s3://test/preset.zarr", variable="var", lod_grids={1: (2, 2)}
    )
    grids = get_lod_grids(product)
    assert grids == {1: (2, 2)}
    assert not opened


def test_storage_options_s3_defaults_anon(monkeypatch):
    monkeypatch.delenv("S3_ANON", raising=False)
    assert _storage_options("s3://bucket/path.zarr") == {"anon": True}


def test_storage_options_s3_anon_disabled_via_env(monkeypatch):
    monkeypatch.setenv("S3_ANON", "false")
    assert _storage_options("s3://bucket/path.zarr") == {"anon": False}


def test_storage_options_non_s3_returns_empty():
    assert _storage_options("https://example.com/data.zarr") == {}
    assert _storage_options("file:///tmp/data.zarr") == {}
    assert _storage_options("/tmp/data.zarr") == {}
