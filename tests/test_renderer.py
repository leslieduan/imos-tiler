import numpy as np
import pytest
import xarray as xr

import app.services.caching.processed_cache as processed_cache_module
from app.services.product.manifest import render_manifest
from app.services.product.product import Product
from app.services.rendering.data_tiles import render_tile


def _make_ds(variables: list[str]) -> xr.Dataset:
    lat = np.linspace(-40, -30, 16)
    lon = np.linspace(140, 155, 16)
    return xr.Dataset(
        {
            v: xr.DataArray(
                np.random.rand(16, 16),
                dims=["lat", "lon"],
                coords={"lat": lat, "lon": lon},
            )
            for v in variables
        }
    )


SCALAR_PRODUCT = Product(
    id="test_scalar",
    source_path="",
    variable="sst",
    lod_grids={1: (1, 1)},
    chunk_px=(8, 8),
    padding=0,
)

UV_PRODUCT = Product(
    id="test_uv",
    source_path="",
    variable=["u", "v"],
    lod_grids={1: (1, 1)},
    chunk_px=(8, 8),
    padding=0,
)


@pytest.fixture(autouse=True)
def clear_processed_cache():
    processed_cache_module._processed_cache.clear()
    yield
    processed_cache_module._processed_cache.clear()


def test_render_tile_scalar_is_valid_png():
    ds = _make_ds(["sst"])
    png = render_tile(SCALAR_PRODUCT, lambda: ds, 1, 0, 0, "2024-01-01")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_tile_uv_is_valid_png():
    ds = _make_ds(["u", "v"])
    png = render_tile(UV_PRODUCT, lambda: ds, 1, 0, 0, "2024-01-01")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_manifest_scalar_shape():
    manifest = render_manifest(SCALAR_PRODUCT, _make_ds(["sst"]))
    assert set(manifest) >= {"bounds", "valueRange", "lods"}
    assert len(manifest["valueRange"]) == 2
    bounds = manifest["bounds"]
    assert bounds["lonMin"] < bounds["lonMax"]
    assert bounds["latMin"] < bounds["latMax"]


def test_render_manifest_uv_shape():
    manifest = render_manifest(UV_PRODUCT, _make_ds(["u", "v"]))
    assert set(manifest) >= {"bounds", "uRange", "vRange", "lods"}
    assert "valueRange" not in manifest
