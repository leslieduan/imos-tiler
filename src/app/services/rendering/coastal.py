"""Coastal fill for sparse data-tile products (e.g. GSLA at 0.2° ≈ 22 km/cell).

Two pieces, both opt-in per product via ``Product.coastal_fill``:

  * ``inpaint_nearest`` — extends valid data toward the coast by copying the
    nearest valid value into NaN cells within ``max_dist_px``. The coastal gap is
    at the *edge* of the data (extrapolation), so plain interpolation can't close
    it; nearest-valid fill can, while the distance cap keeps us from fabricating
    values far from any real measurement.
  * ``land_mask_for_grid`` — a boolean land mask sampled from the committed global
    Natural Earth raster (src/app/assets/land_mask.npz) onto a render grid, so the caller can
    cut fabricated values back off the land. Reuses the exact lon/lat → pixel
    mapping the resample/shader assume (linspace over the grid bounds, north→south).

No new runtime deps: numpy + scipy only (scipy already required).
"""

import logging
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.ndimage import distance_transform_edt

from app.config.paths import LAND_MASK_PATH

logger = logging.getLogger(__name__)

# Loaded lazily so import (and tests that never touch land) don't pay the unpack,
# and so a missing asset only fails the products that actually opt in.
_land_mask: np.ndarray | None = None
_land_meta: dict[str, float] | None = None


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
        logger.debug(
            "[coastal] land mask loaded",
            extra={"shape": shape, "land_frac": round(float(_land_mask.mean()), 3)},
        )
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
