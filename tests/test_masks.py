"""Grid-mask tests: nearest-valid inpaint, land-mask and ocean-mask sampling, and
the fill+cut integration through _compute_processed.

These exercise the real committed mask assets (src/app/assets/land_mask.npz and
ocean_mask.npz), so the geographic assertions double as a smoke test that the
assets are present and sane.
"""

import numpy as np
import xarray as xr

from app.services.product.product import CoastalFill, Product
from app.services.rendering.data_tiles import _compute_processed
from app.services.rendering.masks import (
    inpaint_nearest,
    land_mask_for_grid,
    load_land_mask,
    load_ocean_mask,
    ocean_mask_for_grid,
)

# --- inpaint_nearest ------------------------------------------------------


def test_inpaint_fills_within_distance_only():
    arr = np.full((5, 5), np.nan, dtype=np.float32)
    arr[2, 2] = 10.0
    filled = inpaint_nearest(arr, max_dist_px=1)
    # Center + its 4 edge neighbours (distance 1) fill; diagonals (~1.41) do not.
    assert (~np.isnan(filled)).sum() == 5
    assert filled[2, 2] == 10.0
    assert filled[1, 2] == 10.0  # neighbour got the nearest valid value
    assert np.isnan(filled[1, 1])  # diagonal beyond distance 1


def test_inpaint_large_distance_fills_everything():
    arr = np.full((5, 5), np.nan, dtype=np.float32)
    arr[2, 2] = 7.0
    filled = inpaint_nearest(arr, max_dist_px=10)
    assert not np.isnan(filled).any()
    assert (filled == 7.0).all()


def test_inpaint_noop_paths_return_input_unchanged():
    valid = np.ones((4, 4), dtype=np.float32)
    assert inpaint_nearest(valid, 5) is valid  # nothing to fill
    nan = np.full((4, 4), np.nan, dtype=np.float32)
    assert inpaint_nearest(nan, 5) is nan  # nothing valid to fill from
    one = np.array([[np.nan, 1.0]], dtype=np.float32)
    assert inpaint_nearest(one, 0) is one  # zero distance disables fill


# --- land_mask_for_grid ---------------------------------------------------


def _land_at(lon: float, lat: float) -> bool:
    """Sample the global mask at a single point via a 1x1 grid."""
    return bool(land_mask_for_grid(lon, lon, lat, lat, 1, 1)[0, 0])


def test_land_mask_known_points():
    assert _land_at(133.9, -23.7) is True  # central Australia
    assert _land_at(-0.1, 51.5) is True  # London
    assert _land_at(160.0, -40.0) is False  # Tasman Sea
    assert _land_at(-140.0, 0.0) is False  # mid Pacific


def test_land_mask_antimeridian_wraps():
    # GSLA spans to 185°E; 182°E must wrap to -178°E (open Pacific), not clip to 180.
    assert _land_at(182.0, 0.0) is False
    # Sanity: the global mask is not degenerate.
    land, _ = load_land_mask()
    assert 0.2 < float(land.mean()) < 0.5


# --- ocean_mask_for_grid --------------------------------------------------


def _ocean_valid_at(lon: float, lat: float) -> bool:
    """Sample the ocean-validity mask at a single point via a 1x1 grid."""
    return bool(ocean_mask_for_grid(lon, lon, lat, lat, 1, 1)[0, 0])


def test_ocean_mask_known_points():
    assert _ocean_valid_at(150.0, -40.0) is True  # open Southern Ocean, valid
    assert _ocean_valid_at(110.4, -2.4) is False  # Borneo land, masked out
    assert _ocean_valid_at(137.0, -6.4) is False  # New Guinea land, masked out
    # Sanity: the regional mask is mostly valid ocean but not degenerate.
    mask, _ = load_ocean_mask()
    assert 0.9 < float(mask.mean()) < 1.0


def test_ocean_mask_out_of_domain_is_invalid():
    # The mask covers lon 50–190°E, lat −60–10°. Anything outside drops out,
    # matching the original reindex-nearest semantics.
    assert _ocean_valid_at(200.0, -40.0) is False  # lon past the eastern edge
    assert _ocean_valid_at(60.0, 30.0) is False  # lat north of the domain


# --- integration through _compute_processed -------------------------------


def _ds_over(lon_min, lon_max, lat_max, lat_min, n=20, fill=0.5):
    """Synthetic single-variable dataset on a regular grid, north→south lat."""
    lat = np.linspace(lat_max, lat_min, n)
    lon = np.linspace(lon_min, lon_max, n)
    data = np.full((n, n), fill, dtype=np.float32)
    return xr.Dataset(
        {"GSLA": xr.DataArray(data, dims=["lat", "lon"], coords={"lat": lat, "lon": lon})}
    )


def _product(coastal_fill, product_id="t"):
    return Product(
        id=product_id,
        source_path="",
        variable="GSLA",
        lod_grids={1: (2, 2)},
        chunk_px=(8, 8),
        padding=0,
        coastal_fill=coastal_fill,
    )


def test_compute_fills_ocean_gap_when_enabled():
    # All-ocean region (Southern Ocean) with a NaN hole in the middle.
    ds = _ds_over(150, 160, -40, -45)
    ds["GSLA"].values[8:12, 8:12] = np.nan

    _, ocean_off = _compute_processed(_product(None), ds, 1)
    _, ocean_on = _compute_processed(_product(CoastalFill(max_dist_px=16)), ds, 1)

    assert ocean_off.sum() < ocean_on.sum()  # the gap was transparent before
    assert ocean_on.all()  # fully filled, and no land here to cut


def test_compute_cuts_land_when_enabled():
    # Region straddling the Australian coast, all-valid data (no NaN to fill).
    ds = _ds_over(130, 145, -20, -35)
    _, ocean = _compute_processed(_product(CoastalFill(max_dist_px=4)), ds, 1)

    grid_h, grid_w = ocean.shape
    land = land_mask_for_grid(130.0, 145.0, -35.0, -20.0, grid_w, grid_h)
    assert land.any() and not land.all()  # the grid really spans coast
    # Every land pixel is cut to transparent; every ocean pixel stays valid.
    assert ocean[land].sum() == 0
    assert ocean[~land].all()


def test_compute_applies_ocean_mask_only_for_currents_product():
    # Region over the Indonesian archipelago: spans valid ocean and masked-out
    # land (per the committed ocean mask), all-valid data (no NaN to fill).
    # The mask is hard-coded to the currents product id, not a config flag.
    ds = _ds_over(105, 140, 0, -12)

    _, ocean_off = _compute_processed(_product(None, product_id="other"), ds, 1)
    _, ocean_on = _compute_processed(
        _product(None, product_id="model_sea_level_anomaly_gridded_realtime_vcur_ucur"), ds, 1
    )

    assert ocean_off.all()  # no cut when the mask is off
    grid_h, grid_w = ocean_on.shape
    expected = ocean_mask_for_grid(105.0, 140.0, -12.0, 0.0, grid_w, grid_h)
    assert expected.any() and not expected.all()  # the grid really straddles the mask edge
    # With all-valid data, the cut validity is exactly the ocean mask.
    np.testing.assert_array_equal(ocean_on, expected)
