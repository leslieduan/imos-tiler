import numpy as np
import pytest
import xarray as xr

import services.data_renderer as renderer_module
from constants import Product
from services.data_renderer import render_manifest, render_tile


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
    renderer_module._processed_cache.clear()
    yield
    renderer_module._processed_cache.clear()


def test_render_tile_scalar_is_valid_png():
    png = render_tile(SCALAR_PRODUCT, _make_ds(["sst"]), 1, 0, 0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_tile_uv_is_valid_png():
    png = render_tile(UV_PRODUCT, _make_ds(["u", "v"]), 1, 0, 0)
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
