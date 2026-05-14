"""
Tile renderer.

Uses the same processed grid cache pattern as renderer.py:
  - _get_processed ressamples the full LOD grid once per (date, lod) and caches the
    final numpy arrays — identical to the NetCDF processed cache.
  - The dataset slice is already fully in RAM (computed by loader), so the resample
    is pure CPU with no S3 I/O.
  - _extract_chunk and _to_png_bytes are imported from renderer.py to avoid duplication.
"""

import logging
import math
import os
import threading
from io import BytesIO

import numpy as np
import xarray as xr
from cachetools import LRUCache
from PIL import Image

from constants import LOD_ZOOM_THRESHOLDS, Product

logger = logging.getLogger(__name__)

# Caches the full resampled grid arrays for a (ds, lod) pair so that all tile
# requests for the same date+LOD share one resample instead of each repeating it.
# Key is (id(ds), lod): each product's ds is a distinct object from _slice_cache, so
# id(ds) implicitly encodes the product. id reuse is impossible because _slice_cache
# holds a strong reference to every ds, preventing GC for the cache's lifetime.
# PROCESSED_CACHE_SIZE should be >= SLICE_CACHE_SIZE × number of LOD levels so that
# every cached slice can have its processed grids cached too.
_PROCESSED_CACHE_SIZE = int(os.environ.get("PROCESSED_CACHE_SIZE", 400))
_processed_cache: LRUCache = LRUCache(maxsize=_PROCESSED_CACHE_SIZE)
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


def _compute_scalar(product: Product, ds: xr.Dataset, lod: int) -> tuple[np.ndarray, np.ndarray]:
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    val_min = float(ds[product.variable].min(skipna=True).values)
    val_max = float(ds[product.variable].max(skipna=True).values)
    if val_max == val_min:
        val_max = val_min + 1.0

    raw = _resample_to_grid(ds[[product.variable]], total_w, total_h)[
        product.variable
    ].values.squeeze()
    ocean = (~np.isnan(raw)).astype(np.uint8)
    val_24 = np.clip(
        (np.nan_to_num(raw, nan=0.0) - val_min) / (val_max - val_min) * 16777215,
        0,
        16777215,
    ).astype(np.uint32)
    return val_24, ocean


def _compute_uv(
    product: Product, ds: xr.Dataset, lod: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    u_var, v_var = product.variable
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    u_min = float(ds[u_var].min(skipna=True).values)
    u_max = float(ds[u_var].max(skipna=True).values)
    v_min = float(ds[v_var].min(skipna=True).values)
    v_max = float(ds[v_var].max(skipna=True).values)
    if u_max == u_min:
        u_max = u_min + 1.0
    if v_max == v_min:
        v_max = v_min + 1.0

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


def _get_processed(product: Product, ds: xr.Dataset, lod: int, date: str) -> tuple:
    key = (product.source_path, date, str(product.variable), lod)

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
        event.wait()

    try:
        result = (
            _compute_uv(product, ds, lod)
            if isinstance(product.variable, list)
            else _compute_scalar(product, ds, lod)
        )
        with _processed_lock:
            _processed_cache[key] = result
        return result
    finally:
        with _processed_lock:
            del _processed_inflight[key]
        event.set()


def evict_processed_cache(product: Product) -> None:
    with _processed_lock:
        keys_to_remove = [k for k in _processed_cache if k[0] == product.source_path]
        for k in keys_to_remove:
            del _processed_cache[k]
    if keys_to_remove:
        logger.info(
            "Processed cache evicted %d entry/entries for: %s", len(keys_to_remove), product.id
        )


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

    pad_top = padding if row_s == 0 else 0
    pad_bottom = padding if row_s + ch == total_h else 0
    pad_left = padding if col_s == 0 else 0
    pad_right = padding if col_s + cw == total_w else 0

    if pad_top or pad_bottom or pad_left or pad_right:
        chunk = np.pad(chunk, ((pad_top, pad_bottom), (pad_left, pad_right)), mode="edge")

    return chunk


def _to_png_bytes(img_array: np.ndarray) -> bytes:
    buf = BytesIO()
    # optimize=False: RGBA bytes must be written exactly as-is for shader decoding
    Image.fromarray(img_array, "RGBA").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def render_tile(product: Product, ds: xr.Dataset, lod: int, cx: int, cy: int, date: str) -> bytes:
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    if isinstance(product.variable, list):
        u_norm, v_norm, ocean = _get_processed(product, ds, lod, date)
        chunk_u = _extract_chunk(
            u_norm, cx, cy, total_w, total_h, product.chunk_px, product.padding
        )
        chunk_v = _extract_chunk(
            v_norm, cx, cy, total_w, total_h, product.chunk_px, product.padding
        )
        chunk_m = _extract_chunk(ocean, cx, cy, total_w, total_h, product.chunk_px, product.padding)
        h, w = chunk_u.shape
        img = np.zeros((h, w, 4), dtype=np.uint8)
        img[:, :, 0] = chunk_u
        img[:, :, 1] = chunk_v
        img[:, :, 2] = chunk_m * 255
        img[:, :, 3] = 255
    else:
        val_24, ocean = _get_processed(product, ds, lod, date)
        chunk_24 = _extract_chunk(
            val_24, cx, cy, total_w, total_h, product.chunk_px, product.padding
        )
        chunk_m = _extract_chunk(ocean, cx, cy, total_w, total_h, product.chunk_px, product.padding)
        h, w = chunk_24.shape
        img = np.zeros((h, w, 4), dtype=np.uint8)
        img[:, :, 0] = (chunk_24 >> 16) & 0xFF
        img[:, :, 1] = (chunk_24 >> 8) & 0xFF
        img[:, :, 2] = chunk_24 & 0xFF
        img[:, :, 3] = chunk_m * 255
        img[chunk_m == 0, :3] = 0

    return _to_png_bytes(img)


def _json_float(v) -> float | None:
    f = float(v)
    return None if math.isnan(f) or math.isinf(f) else f


def render_manifest(product: Product, ds: xr.Dataset) -> dict:
    lon_min_g = float(ds.lon.min())
    lon_max_g = float(ds.lon.max())
    lat_min_g = float(ds.lat.min())
    lat_max_g = float(ds.lat.max())

    bounds = {"lonMin": lon_min_g, "lonMax": lon_max_g, "latMin": lat_min_g, "latMax": lat_max_g}
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

    if isinstance(product.variable, list):
        u_var, v_var = product.variable
        return {
            "bounds": bounds,
            "uRange": [
                _json_float(ds[u_var].min(skipna=True).values),
                _json_float(ds[u_var].max(skipna=True).values),
            ],
            "vRange": [
                _json_float(ds[v_var].min(skipna=True).values),
                _json_float(ds[v_var].max(skipna=True).values),
            ],
            "lods": lod_meta,
        }
    return {
        "bounds": bounds,
        "valueRange": [
            _json_float(ds[product.variable].min(skipna=True).values),
            _json_float(ds[product.variable].max(skipna=True).values),
        ],
        "lods": lod_meta,
    }
