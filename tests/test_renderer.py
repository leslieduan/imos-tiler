import threading
import time

import numpy as np
import xarray as xr

import app.services.rendering.data_tiles as data_tiles_module
from app.services.product.manifest import render_manifest
from app.services.product.product import Product
from app.services.rendering.data_tiles import render_tile
from app.services.rendering.kernels import resample_variables_to_grid


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

# 2x2 grid of 8px chunks so two different tiles (cx, cy) map to the same
# processed-grid key — used to test that concurrent renders of different
# tiles at the same (product, date, lod) share one _compute_processed call.
MULTI_TILE_PRODUCT = Product(
    id="test_multi_tile",
    source_path="",
    variable="sst",
    lod_grids={1: (2, 2)},
    chunk_px=(8, 8),
    padding=0,
)


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


def _make_categorical_ds(flag_meanings: str | None = "none moderate strong severe extreme"):
    ds = _make_ds(["cat"])
    ds["cat"].attrs["flag_values"] = [0, 1, 2, 3, 4]
    if flag_meanings is not None:
        ds["cat"].attrs["flag_meanings"] = flag_meanings
    return ds


CATEGORICAL_PRODUCT = Product(
    id="test_cat",
    source_path="",
    variable="cat",
    lod_grids={1: (1, 1)},
    chunk_px=(8, 8),
    padding=0,
)


def test_render_manifest_categorical_includes_flag_values_and_meanings():
    manifest = render_manifest(CATEGORICAL_PRODUCT, _make_categorical_ds())
    assert manifest["flagValues"] == [0, 1, 2, 3, 4]
    assert manifest["flagMeanings"] == ["none", "moderate", "strong", "severe", "extreme"]
    # The scalar value range is still emitted alongside the categorical fields.
    assert len(manifest["valueRange"]) == 2


def test_render_manifest_continuous_has_no_flag_fields():
    manifest = render_manifest(SCALAR_PRODUCT, _make_ds(["sst"]))
    assert "flagValues" not in manifest
    assert "flagMeanings" not in manifest


def test_render_manifest_categorical_omits_misaligned_meanings():
    # 2 labels for 5 values → flag_meanings is dropped, flagValues still present.
    ds = _make_categorical_ds(flag_meanings="only two")
    manifest = render_manifest(CATEGORICAL_PRODUCT, ds)
    assert manifest["flagValues"] == [0, 1, 2, 3, 4]
    assert "flagMeanings" not in manifest


# --- resampling: categorical → nearest, continuous → bilinear ---------------


def _two_by_two_ds(variable: str, flag_values: list[int] | None) -> xr.Dataset:
    # Sharp 0/4 checkerboard so blended values (1/2/3) are unmistakable if they appear.
    arr = np.array([[0.0, 4.0], [4.0, 0.0]], dtype="float32")
    da = xr.DataArray(arr, dims=["lat", "lon"], coords={"lat": [1.0, 0.0], "lon": [0.0, 1.0]})
    if flag_values is not None:
        da.attrs["flag_values"] = flag_values
    return xr.Dataset({variable: da})


def test_resample_categorical_uses_nearest_no_blended_codes():
    ds = _two_by_two_ds("cat", flag_values=[0, 4])
    (out,) = resample_variables_to_grid(ds, ["cat"], 8, 8)
    # Nearest must reproduce only the source codes — never an interpolated 1/2/3.
    assert set(np.unique(out)).issubset({0.0, 4.0})


def test_resample_continuous_uses_bilinear_blends():
    ds = _two_by_two_ds("cont", flag_values=None)
    (out,) = resample_variables_to_grid(ds, ["cont"], 8, 8)
    # Bilinear must produce intermediate values absent from the source set.
    assert not set(np.unique(out)).issubset({0.0, 4.0})


def test_render_tile_categorical_is_valid_png():
    # End-to-end data-tile render of a categorical product must not crash and
    # must produce a valid PNG (resample is nearest under the hood).
    png = render_tile(CATEGORICAL_PRODUCT, _make_categorical_ds, 1, 0, 0, "2024-01-01")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --- concurrent stampede protection (always in-process, independent of CACHE_BACKEND) ---


def test_concurrent_tiles_at_same_lod_share_one_processed_compute(monkeypatch):
    """Two different tiles (cx, cy) in the same (product, date, lod) grid must
    share one _compute_processed call when requested concurrently, not each
    redo the resample independently. This is what `_processed_dedup`
    (services.caching.deduper) protects — see its docstring for why this
    matters even under CACHE_BACKEND=none."""
    ds = _make_ds(["sst"])
    calls = 0
    proceed = threading.Event()
    real_compute = data_tiles_module._compute_processed

    def slow_compute(*args, **kwargs):
        nonlocal calls
        calls += 1
        proceed.wait(timeout=2)
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(data_tiles_module, "_compute_processed", slow_compute)

    results: list[bytes] = []

    def render(cx, cy):
        results.append(render_tile(MULTI_TILE_PRODUCT, lambda: ds, 1, cx, cy, "2024-01-01"))

    threads = [
        threading.Thread(target=render, args=(0, 0)),
        threading.Thread(target=render, args=(1, 1)),
    ]
    for t in threads:
        t.start()
    time.sleep(0.1)  # let both threads register on the in-flight key
    proceed.set()
    for t in threads:
        t.join(timeout=2)

    assert calls == 1, "expected exactly one compute; the rest should share it via _processed_dedup"
    assert len(results) == 2
    assert all(body[:8] == b"\x89PNG\r\n\x1a\n" for body in results)
