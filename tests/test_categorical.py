"""Categorical (flag-valued) rendering: scheme resolution and discrete tile render.

The MCS_category product is categorical (CF flag_values [0..4]) — these lock the
contract that such variables render as discrete blocks via a value-indexed LUT
with nearest-neighbour resampling, never an interpolated continuous ramp.
"""

import io

import numpy as np
import xarray as xr
from PIL import Image

from app.services.colormap.categorical import (
    DEFAULT_CATEGORICAL_PALETTE,
    is_categorical_variable,
    resolve_scheme,
)
from app.services.rendering.visual_tiles import render_tile
from app.utils.colors import build_categorical_lut

_MCS_COLORS = DEFAULT_CATEGORICAL_PALETTE


def _make_categorical_ds() -> xr.Dataset:
    lat = np.linspace(-40, -30, 8)
    lon = np.linspace(140, 150, 8)
    # Cover every category 0..4 across the grid.
    data = (np.arange(64) % 5).reshape(8, 8).astype("float32")
    da = xr.DataArray(data, dims=["lat", "lon"], coords={"lat": lat, "lon": lon})
    da.attrs["flag_values"] = [0, 1, 2, 3, 4]
    da.attrs["flag_meanings"] = "none moderate strong severe extreme"
    return xr.Dataset({"MCS_category": da})


def _make_continuous_ds() -> xr.Dataset:
    lat = np.linspace(-40, -30, 8)
    lon = np.linspace(140, 150, 8)
    return xr.Dataset(
        {
            "GSLA": xr.DataArray(
                np.random.rand(8, 8), dims=["lat", "lon"], coords={"lat": lat, "lon": lon}
            )
        }
    )


# --- categorical detection (hoisted out of resolve_scheme) ----------------


def test_is_categorical_variable():
    assert is_categorical_variable(_make_categorical_ds()["MCS_category"].attrs) is True
    assert is_categorical_variable(_make_continuous_ds()["GSLA"].attrs) is False


# --- scheme resolution ----------------------------------------------------


def test_mcs_default_scheme():
    ds = _make_categorical_ds()
    scheme = resolve_scheme(ds["MCS_category"].attrs, "viridis")
    assert scheme.values == (0, 1, 2, 3, 4)
    assert scheme.labels == ("none", "moderate", "strong", "severe", "extreme")
    assert scheme.colors == tuple(_MCS_COLORS)
    # "none" is transparent so the basemap shows through.
    assert scheme.colors[0][3] == 0


def test_scheme_lut_is_value_indexed():
    ds = _make_categorical_ds()
    scheme = resolve_scheme(ds["MCS_category"].attrs, "viridis")
    lut = scheme.lut()
    assert lut[1] == _MCS_COLORS[1]  # value 1 → moderate, not a rescaled slot
    assert lut[4] == _MCS_COLORS[4]
    assert lut[0] == (0, 0, 0, 0)
    assert lut[200] == (0, 0, 0, 0)  # unmapped codes transparent


def test_flag_colors_attr_beats_default_palette():
    ds = _make_categorical_ds()
    ds["MCS_category"].attrs["flag_colors"] = "#000000 #ffffff #ff0000 #00ff00 #0000ff"
    scheme = resolve_scheme(ds["MCS_category"].attrs, "viridis")
    assert scheme.colors[2] == (255, 0, 0, 255)  # from flag_colors, not the blue default


def test_explicit_categorical_colormap_beats_flag_colors(monkeypatch):
    import app.services.colormap.registry as reg

    # A real categorical LUT keyed on the same values 0..4 (as registration enforces).
    categories = {
        0: [10, 20, 30, 255],
        1: [40, 50, 60, 255],
        2: [70, 80, 90, 255],
        3: [11, 12, 13, 255],
        4: [14, 15, 16, 255],
    }
    lut = build_categorical_lut(categories, (0.0, 4.0))
    monkeypatch.setattr(reg, "_custom_colormaps", {"my_cat": [tuple(c) for c in lut]})
    monkeypatch.setattr(reg, "_custom_colormap_modes", {"my_cat": "categorical"})

    ds = _make_categorical_ds()
    ds["MCS_category"].attrs["flag_colors"] = "#ff0000 #ff0000 #ff0000 #ff0000 #ff0000"
    scheme = resolve_scheme(ds["MCS_category"].attrs, "my_cat")
    # rule 1 wins over flag_colors, mapped per value (incl. value 0).
    assert scheme.colors[0] == (10, 20, 30, 255)
    assert scheme.colors[4] == (14, 15, 16, 255)


def test_misaligned_flag_meanings_dropped():
    ds = _make_categorical_ds()
    ds["MCS_category"].attrs["flag_meanings"] = "only two"  # 2 labels for 5 values
    scheme = resolve_scheme(ds["MCS_category"].attrs, "viridis")
    assert scheme.labels is None


# --- discrete tile rendering ----------------------------------------------


def _opaque_colors(png: bytes) -> set[tuple[int, int, int]]:
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    arr = np.array(img)
    opaque = arr[arr[..., 3] > 0]
    return {(int(px[0]), int(px[1]), int(px[2])) for px in opaque}


def test_categorical_tile_uses_only_palette_colors():
    """No interpolated in-between colours — proves nearest resampling + discrete LUT.

    A bilinear path would blend adjacent category codes and emit colours that are
    not in the palette; this asserts every opaque pixel is an exact palette colour.
    """
    ds = _make_categorical_ds()
    png = render_tile(ds, "MCS_category", 0, 0, 0, fmt="png")  # no colormap → default palette
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    palette_rgb = {c[:3] for c in _MCS_COLORS if c[3] > 0}
    assert _opaque_colors(png).issubset(palette_rgb)


def test_categorical_tile_rejects_webp():
    ds = _make_categorical_ds()
    try:
        render_tile(ds, "MCS_category", 0, 0, 0, fmt="webp")
        raise AssertionError("expected ValueError for categorical WebP")
    except ValueError as e:
        assert "webp" in str(e).lower()


def test_categorical_tile_rejects_continuous_colormap():
    ds = _make_categorical_ds()
    try:
        render_tile(ds, "MCS_category", 0, 0, 0, colormap_name="plasma", fmt="png")
        raise AssertionError("expected ValueError for continuous colormap on categorical")
    except ValueError as e:
        assert "categorical" in str(e).lower() and "plasma" in str(e)
