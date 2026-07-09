"""Grid masks and coastal fill for data-tile products.

  * ``inpaint_nearest`` — coastal fill (opt-in via ``Product.coastal_fill``) for
    sparse products (e.g. GSLA at 0.2° ≈ 22 km/cell): extends valid data toward
    the coast by copying the nearest valid value into NaN cells within
    ``max_dist_px``. The coastal gap is at the *edge* of the data (extrapolation),
    so plain interpolation can't close it; nearest-valid fill can, while the
    distance cap keeps us from fabricating values far from any real measurement.
    Applied in ``data_tiles._compute_processed``.
  * ``land_mask_for_grid`` — a boolean land mask sampled from the committed global
    Natural Earth raster (src/app/assets/land_mask.npz) onto a render grid, so the
    caller can cut fabricated values back off the land. Reuses the exact lon/lat →
    pixel mapping the resample/shader assume (linspace over the grid bounds,
    north→south). Applied in ``data_tiles._compute_processed``.
  * ``apply_ocean_mask`` — a regional ocean-validity mask
    (src/app/assets/ocean_mask.npz) used to null anomalous values outside the valid
    model domain. Unlike the two above this is applied to the *raw slice* at read
    time (opt-in via ``Product.ocean_masked``), not on a render grid: cutting the
    anomalies at the source — before resampling can bleed them into valid
    neighbours, and before point lookups read them — means every consumer inherits
    the cut for free. Built from the model_sea_level_anomaly_gridded_realtime grid,
    so only products on that store should opt in.

No new runtime deps: numpy + scipy only (scipy already required). The .npz assets
are pre-baked by scripts (no netCDF engine at runtime).
"""

from functools import lru_cache
from pathlib import Path

import numpy as np
import xarray as xr
from scipy.ndimage import distance_transform_edt

from app.config.paths import LAND_MASK_PATH, OCEAN_MASK_PATH

# Loaded lazily so import (and tests that never touch land) don't pay the unpack,
# and so a missing asset only fails the products that actually opt in.
_land_mask: np.ndarray | None = None
_land_meta: dict[str, float] | None = None
_ocean_mask: np.ndarray | None = None
_ocean_meta: dict[str, float] | None = None


def load_land_mask() -> tuple[np.ndarray, dict[str, float]]:
    """Global boolean land grid (True = land), north→south, plus its geo metadata.

    Cached after first load. The asset is bit-packed on disk (~3 MB); we unpack to
    a full bool grid (~26 MB resident) once.
    """
    global _land_mask, _land_meta
    if _land_mask is None:
        path = Path(LAND_MASK_PATH)
        if not path.exists():
            raise FileNotFoundError(
                f"Land-mask asset not found at {path}. Generate it with "
                "`uv run --with regionmask --with cartopy --with pooch "
                "python scripts/build_land_mask.py`."
            )
        with np.load(path) as npz:
            shape = tuple(int(x) for x in npz["shape"])
            n = shape[0] * shape[1]
            _land_mask = np.unpackbits(npz["packed"])[:n].astype(bool).reshape(shape)
            _land_meta = {
                "res": float(npz["res"]),
                "lon_min": float(npz["lon_min"]),
                "lat_max": float(npz["lat_max"]),
            }
    assert _land_meta is not None
    return _land_mask, _land_meta


@lru_cache(maxsize=64)
def land_mask_for_grid(
    lon_min: float, lon_max: float, lat_min: float, lat_max: float, total_w: int, total_h: int
) -> np.ndarray:
    """Boolean land mask (True = land) on a (total_h, total_w) render grid.

    Target coordinates follow ``linspace(lon_min, lon_max, total_w)`` and
    ``linspace(lat_max, lat_min, total_h)`` — the same mapping
    ``resample_variables_to_grid`` and the WebGL shader use (docs/technical.md §5.6),
    so the cut lines up with the rendered pixels. Longitudes are wrapped into
    [-180, 180) so antimeridian-straddling domains (GSLA spans 57–185°E) index the
    global mask correctly.

    Result is cached: it's static per (product grid), independent of date/data.
    """
    land, meta = load_land_mask()
    h_src, w_src = land.shape
    res = meta["res"]

    lons = np.linspace(lon_min, lon_max, total_w)
    lats = np.linspace(lat_max, lat_min, total_h)  # north → south
    lons = ((lons + 180.0) % 360.0) - 180.0  # wrap to [-180, 180)

    cols = np.floor((lons - meta["lon_min"]) / res).astype(np.intp)
    rows = np.floor((meta["lat_max"] - lats) / res).astype(np.intp)
    np.clip(cols, 0, w_src - 1, out=cols)
    np.clip(rows, 0, h_src - 1, out=rows)

    return land[np.ix_(rows, cols)]


def load_ocean_mask() -> tuple[np.ndarray, dict[str, float]]:
    """Regional boolean ocean-validity grid (True = valid ocean), north→south,
    plus its geo metadata. Cached after first load.

    Built from src/app/assets/OCmask.nc by scripts/build_ocean_mask.py; stored as
    a bit-packed grid covering the model sea-level domain (lon 50–190°E,
    lat −60–10°). Used to cut anomalous current values outside the valid mask.
    """
    global _ocean_mask, _ocean_meta
    if _ocean_mask is None:
        path = Path(OCEAN_MASK_PATH)
        if not path.exists():
            raise FileNotFoundError(
                f"Ocean-mask asset not found at {path}. Generate it with "
                "`uv run --with h5netcdf --with h5py python scripts/build_ocean_mask.py`."
            )
        with np.load(path) as npz:
            shape = tuple(int(x) for x in npz["shape"])
            n = shape[0] * shape[1]
            _ocean_mask = np.unpackbits(npz["packed"])[:n].astype(bool).reshape(shape)
            _ocean_meta = {
                "res": float(npz["res"]),
                "lon_min": float(npz["lon_min"]),
                "lat_max": float(npz["lat_max"]),
            }
    assert _ocean_meta is not None
    return _ocean_mask, _ocean_meta


def ocean_valid_for_coords(lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    """Boolean ocean-validity mask (True = valid) for an explicit lon/lat grid.

    Samples the committed regional mask at each ``(lat, lon)`` via nearest grid
    point (round), marking any point outside the mask domain invalid — matching
    the original ``reindex nearest`` semantics where out-of-domain points dropped
    out. Returns a ``(len(lats), len(lons))`` bool array oriented to the inputs.

    Takes the *source* grid's own coordinates rather than a render-grid linspace,
    because the cut now happens on the raw slice (see ``apply_ocean_mask``) before
    any resampling.
    """
    mask, meta = load_ocean_mask()
    h_src, w_src = mask.shape
    res = meta["res"]
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)

    cols = np.round((lons - meta["lon_min"]) / res).astype(np.intp)
    rows = np.round((meta["lat_max"] - lats) / res).astype(np.intp)
    in_bounds_c = (cols >= 0) & (cols < w_src)
    in_bounds_r = (rows >= 0) & (rows < h_src)
    np.clip(cols, 0, w_src - 1, out=cols)
    np.clip(rows, 0, h_src - 1, out=rows)

    valid = mask[np.ix_(rows, cols)]
    # Anything sampled from outside the mask domain is not "valid ocean".
    valid &= in_bounds_r[:, None] & in_bounds_c[None, :]
    return valid


def apply_ocean_mask(ds: xr.Dataset, variables: list[str]) -> xr.Dataset:
    """Return ``ds`` with cells outside the committed ocean-validity mask set to
    NaN, for each named variable.

    Used by the slice layer for products with ``Product.ocean_masked``: the model
    emits anomalous values outside its valid domain, so we null them at the source
    — before resampling/point selection — rather than cutting per render grid.
    """
    valid = ocean_valid_for_coords(ds.lon.values, ds.lat.values)  # (lat, lon)
    valid_da = xr.DataArray(valid, dims=("lat", "lon"), coords={"lat": ds.lat, "lon": ds.lon})
    out = ds.copy()
    for v in variables:
        out[v] = ds[v].where(valid_da)
    return out


def inpaint_nearest(arr: np.ndarray, max_dist_px: int) -> np.ndarray:
    """Fill NaNs in ``arr`` from the nearest non-NaN cell, but only within
    ``max_dist_px`` (Euclidean, in grid pixels). Cells farther than that from any
    valid value stay NaN.

    ``arr`` is float32 (a resampled variable grid); returns a new float32 array.
    """
    invalid = np.isnan(arr)
    if max_dist_px <= 0 or not invalid.any() or invalid.all():
        return arr

    # EDT of the invalid mask gives, for every invalid cell, the distance to and
    # index of the nearest valid cell in one pass.
    dist, (iy, ix) = distance_transform_edt(invalid, return_indices=True)
    fill = invalid & (dist <= max_dist_px)
    if not fill.any():
        return arr

    out = arr.copy()
    out[fill] = arr[iy[fill], ix[fill]]
    return out
