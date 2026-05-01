import logging
import threading
import time
from io import BytesIO

import numpy as np
import xarray as xr
from cachetools import LRUCache
from PIL import Image

from constants import LOD_ZOOM_THRESHOLDS, Product

logger = logging.getLogger(__name__)

# Processed grid cache: stores final numpy arrays ready for _extract_chunk.
# Keyed by (id(ds), lod) — safe because ds is held alive by the loader's LRU cache.
# Merges the old resample + numpy ops into one step: resample happens here but
# only once per (date, lod); all tiles at that lod just call _extract_chunk.
_processed_cache: LRUCache = LRUCache(maxsize=20)
_processed_inflight: dict = {}
_processed_lock = threading.Lock()


def _get_bounds(ds: xr.Dataset) -> tuple[float, float, float, float]:
    """Return (lon_min, lon_max, lat_min, lat_max)."""
    return (
        float(ds.lon.min().values),
        float(ds.lon.max().values),
        float(ds.lat.min().values),
        float(ds.lat.max().values),
    )


def _resample_to_grid(ds: xr.Dataset, total_w: int, total_h: int) -> xr.Dataset:
    # The source NetCDF grid points don't align with the target pixel positions,
    # so we interpolate: for each of the total_w×total_h output pixels, scipy finds
    # the surrounding source points and computes a weighted average (bilinear).
    # This covers the full LOD grid (all chunks combined), not a single tile —
    # _extract_chunk then slices the relevant chunk out of the result.
    lon_min, lon_max, lat_min, lat_max = _get_bounds(ds)
    target_lons = np.linspace(lon_min, lon_max, total_w)
    target_lats = np.linspace(lat_max, lat_min, total_h)  # north → south
    result = ds.interp(lon=target_lons, lat=target_lats, method="linear")
    return result


def _compute_scalar_arrays(
    product: Product, ds: xr.Dataset, lod: int
) -> tuple[np.ndarray, np.ndarray]:
    """Resample + normalise → (val_24, ocean) over the full LOD grid."""
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    val_min = float(ds[product.variable].min(skipna=True).values)
    val_max = float(ds[product.variable].max(skipna=True).values)
    if val_max == val_min:
        val_max = val_min + 1.0

    raw = _resample_to_grid(ds[[product.variable]], total_w, total_h)[product.variable].values.squeeze()
    ocean = (~np.isnan(raw)).astype(np.uint8)
    val_24 = np.clip(
        (np.nan_to_num(raw, nan=0.0) - val_min) / (val_max - val_min) * 16777215,
        0, 16777215,
    ).astype(np.uint32)
    return val_24, ocean


def _compute_uv_arrays(
    product: Product, ds: xr.Dataset, lod: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resample + normalise → (u_norm, v_norm, ocean) over the full LOD grid."""
    u_var, v_var = product.variable
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    u_min = float(ds[u_var].min(skipna=True).values)
    u_max = float(ds[u_var].max(skipna=True).values)
    v_min = float(ds[v_var].min(skipna=True).values)
    v_max = float(ds[v_var].max(skipna=True).values)
    if u_max == u_min: u_max = u_min + 1.0
    if v_max == v_min: v_max = v_min + 1.0

    ds_r = _resample_to_grid(ds[[u_var, v_var]], total_w, total_h)
    u_raw = ds_r[u_var].values.squeeze()
    v_raw = ds_r[v_var].values.squeeze()

    ocean = (~np.isnan(u_raw)).astype(np.uint8)
    u_norm = np.clip(
        (np.nan_to_num(u_raw, nan=0.0) - u_min) / (u_max - u_min) * 255, 0, 255
    ).astype(np.uint8)
    v_norm = np.clip(
        (np.nan_to_num(v_raw, nan=0.0) - v_min) / (v_max - v_min) * 255, 0, 255
    ).astype(np.uint8)
    return u_norm, v_norm, ocean


def _get_processed(product: Product, ds: xr.Dataset, lod: int) -> tuple:
    # Resampling the full LOD grid is expensive (e.g. 5.5M pixels for SSTA LOD3).
    # The result is identical for every tile at the same zoom level, so we cache
    # the final numpy arrays here. All tiles at that lod then just call _extract_chunk.
    # variable is included because multiple products can share the same ds (same source
    # file, different variables) — e.g. dhd_mosaic and ssta_mosaic.
    var_key = tuple(product.variable) if isinstance(product.variable, list) else product.variable
    key = (id(ds), var_key, lod)

    while True:
        with _processed_lock:
            cached = _processed_cache.get(key)
            if cached is not None:
                return cached
            if key not in _processed_inflight:
                event = threading.Event()
                _processed_inflight[key] = event
                break
            event = _processed_inflight[key]
        # Another thread is computing this key — wait then re-check cache
        event.wait()

    try:
        if isinstance(product.variable, list):
            result = _compute_uv_arrays(product, ds, lod)
        else:
            result = _compute_scalar_arrays(product, ds, lod)
        with _processed_lock:
            _processed_cache[key] = result
        return result
    finally:
        with _processed_lock:
            del _processed_inflight[key]
        event.set()


def _extract_chunk(
    arr: np.ndarray,
    cx: int,
    cy: int,
    total_w: int,
    total_h: int,
    chunk_px: tuple[int, int],
    padding: int,
) -> np.ndarray:
    cw, ch = chunk_px
    row_s = cy * ch
    col_s = cx * cw

    p_row_s = max(row_s - padding, 0)
    p_row_e = min(row_s + ch + padding, total_h)
    p_col_s = max(col_s - padding, 0)
    p_col_e = min(col_s + cw + padding, total_w)

    chunk = arr[p_row_s:p_row_e, p_col_s:p_col_e]

    pad_top    = padding if row_s == 0            else 0
    pad_bottom = padding if row_s + ch == total_h else 0
    pad_left   = padding if col_s == 0            else 0
    pad_right  = padding if col_s + cw == total_w else 0

    if pad_top or pad_bottom or pad_left or pad_right:
        chunk = np.pad(chunk, ((pad_top, pad_bottom), (pad_left, pad_right)), mode="edge")

    return chunk


def _to_png_bytes(img_array: np.ndarray) -> bytes:
    buf = BytesIO()
    # optimize=False: RGBA bytes must be written exactly as-is for shader decoding
    Image.fromarray(img_array, "RGBA").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _render_scalar_tile(product: Product, ds: xr.Dataset, lod: int, cx: int, cy: int) -> bytes:
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    val_24, ocean = _get_processed(product, ds, lod)

    chunk_24 = _extract_chunk(val_24, cx, cy, total_w, total_h, product.chunk_px, product.padding)
    chunk_m  = _extract_chunk(ocean,  cx, cy, total_w, total_h, product.chunk_px, product.padding)

    h, w = chunk_24.shape
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[:, :, 0] = (chunk_24 >> 16) & 0xFF
    img[:, :, 1] = (chunk_24 >> 8)  & 0xFF
    img[:, :, 2] =  chunk_24         & 0xFF
    img[:, :, 3] = chunk_m * 255
    img[chunk_m == 0, :3] = 0  # premultiplied alpha

    return _to_png_bytes(img)


def _render_ocean_current_tile(product: Product, ds: xr.Dataset, lod: int, cx: int, cy: int) -> bytes:
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    u_norm, v_norm, ocean = _get_processed(product, ds, lod)

    chunk_u = _extract_chunk(u_norm, cx, cy, total_w, total_h, product.chunk_px, product.padding)
    chunk_v = _extract_chunk(v_norm, cx, cy, total_w, total_h, product.chunk_px, product.padding)
    chunk_m = _extract_chunk(ocean,  cx, cy, total_w, total_h, product.chunk_px, product.padding)

    h, w = chunk_u.shape
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[:, :, 0] = chunk_u        # R = U
    img[:, :, 1] = chunk_v        # G = V
    img[:, :, 2] = chunk_m * 255  # B = ocean mask
    img[:, :, 3] = 255            # A = always opaque

    return _to_png_bytes(img)


def render_tile(product: Product, ds: xr.Dataset, lod: int, cx: int, cy: int) -> bytes:
    if isinstance(product.variable, list):
        return _render_ocean_current_tile(product, ds, lod, cx, cy)
    return _render_scalar_tile(product, ds, lod, cx, cy)


def render_manifest(product: Product, ds: xr.Dataset) -> dict:
    lon_min, lon_max, lat_min, lat_max = _get_bounds(ds)
    lod_meta = {
        str(lod): {
            "grid": list(product.lod_grids[lod]),
            "chunkPx": list(product.chunk_px),
            "storedPx": [
                product.chunk_px[0] + 2 * product.padding,
                product.chunk_px[1] + 2 * product.padding,
            ],
            "padding": product.padding,
            **({"zoomThreshold": LOD_ZOOM_THRESHOLDS[lod]} if lod in LOD_ZOOM_THRESHOLDS else {}),
        }
        for lod in product.lod_grids
    }
    bounds = {"lonMin": lon_min, "lonMax": lon_max, "latMin": lat_min, "latMax": lat_max}

    if isinstance(product.variable, list):
        u_var, v_var = product.variable
        return {
            "bounds": bounds,
            "uRange": [float(ds[u_var].min(skipna=True).values), float(ds[u_var].max(skipna=True).values)],
            "vRange": [float(ds[v_var].min(skipna=True).values), float(ds[v_var].max(skipna=True).values)],
            "lods": lod_meta,
        }
    return {
        "bounds": bounds,
        "valueRange": [
            float(ds[product.variable].min(skipna=True).values),
            float(ds[product.variable].max(skipna=True).values),
        ],
        "lods": lod_meta,
    }
