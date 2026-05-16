import concurrent.futures
import logging
import os
import threading
from collections.abc import Callable
from io import BytesIO

import numpy as np
import xarray as xr
from cachetools import LRUCache
from PIL import Image

from constants import LOD, Product
from utils.geo import dataset_bounds, json_safe_float

logger = logging.getLogger(__name__)


_PROCESSED_CACHE_SIZE = int(os.environ.get("PROCESSED_CACHE_SIZE", 50))
_processed_cache: LRUCache = LRUCache(maxsize=_PROCESSED_CACHE_SIZE)
_processed_inflight: dict[tuple, concurrent.futures.Future] = {}
_processed_lock = threading.Lock()


def _resample_to_grid(ds: xr.Dataset, total_w: int, total_h: int) -> xr.Dataset:
    # The source Zarr grid points don't align with the target pixel positions,
    # so we interpolate: for each of the total_w×total_h output pixels, xarray finds
    # the surrounding source points and computes a weighted average (bilinear).
    # This covers the full LOD grid (all chunks combined), not a single tile —
    # _extract_chunk then slices the relevant chunk out of the result.
    lon_min, lon_max, lat_min, lat_max = dataset_bounds(ds)
    target_lons = np.linspace(lon_min, lon_max, total_w)
    target_lats = np.linspace(lat_max, lat_min, total_h)  # north → south
    result = ds.interp(lon=target_lons, lat=target_lats, method="linear")
    return result


def _normalize(arr: np.ndarray, lo: float, hi: float, out_max: int) -> np.ndarray:
    """Normalize arr to [0, out_max], replacing NaN with 0. Returns uint8 or uint32."""
    span = hi - lo if hi != lo else 1.0
    result = np.clip((np.nan_to_num(arr, nan=0.0) - lo) / span * out_max, 0, out_max)
    return result.astype(np.uint32 if out_max > 255 else np.uint8)


def _var_range(ds: xr.Dataset, var: str) -> tuple[float, float]:
    lo = float(ds[var].min(skipna=True).values)
    hi = float(ds[var].max(skipna=True).values)
    return lo, hi if hi != lo else lo + 1.0


def _compute_scalar(product: Product, ds: xr.Dataset, lod: int) -> tuple[np.ndarray, np.ndarray]:
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    val_min, val_max = _var_range(ds, product.variable)
    raw = _resample_to_grid(ds[[product.variable]], total_w, total_h)[
        product.variable
    ].values.squeeze()
    ocean = (~np.isnan(raw)).astype(np.uint8)
    return _normalize(raw, val_min, val_max, 16777215), ocean


def _compute_uv(
    product: Product, ds: xr.Dataset, lod: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    u_var, v_var = product.variable
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    u_min, u_max = _var_range(ds, u_var)
    v_min, v_max = _var_range(ds, v_var)
    ds_r = _resample_to_grid(ds[[u_var, v_var]], total_w, total_h)
    u_raw = ds_r[u_var].values.squeeze()
    v_raw = ds_r[v_var].values.squeeze()

    ocean = (~np.isnan(u_raw)).astype(np.uint8)
    return _normalize(u_raw, u_min, u_max, 255), _normalize(v_raw, v_min, v_max, 255), ocean


def _get_processed(
    product: Product, load_ds: Callable[[], xr.Dataset], lod: int, date: str
) -> tuple:
    """load_ds only called once per (product, date) when the processed grid is not cached yet."""
    key = (product.source_path, date, str(product.variable), lod)

    should_compute = False
    with _processed_lock:
        cached = _processed_cache.get(key)
        if cached is not None:
            return cached
        if key in _processed_inflight:
            future = _processed_inflight[key]
        else:
            future: concurrent.futures.Future = concurrent.futures.Future()
            _processed_inflight[key] = future
            should_compute = True

    if not should_compute:
        return future.result()

    try:
        ds = load_ds()
        result = (
            _compute_uv(product, ds, lod)
            if isinstance(product.variable, list)
            else _compute_scalar(product, ds, lod)
        )
        with _processed_lock:
            _processed_cache[key] = result
        future.set_result(result)
    except Exception as e:
        future.set_exception(e)
        raise
    finally:
        with _processed_lock:
            _processed_inflight.pop(key, None)
    return result


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


def render_tile(
    product: Product, load_ds: Callable[[], xr.Dataset], lod: int, cx: int, cy: int, date: str
) -> bytes:
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    if isinstance(product.variable, list):
        u_norm, v_norm, ocean = _get_processed(product, load_ds, lod, date)
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
        val_24, ocean = _get_processed(product, load_ds, lod, date)
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
            **({"zoomThreshold": LOD.zoom_thresholds[lod]} if lod in LOD.zoom_thresholds else {}),
        }
        for lod in product.lod_grids
    }

    if isinstance(product.variable, list):
        u_var, v_var = product.variable
        return {
            "bounds": bounds,
            "uRange": [
                json_safe_float(ds[u_var].min(skipna=True).values),
                json_safe_float(ds[u_var].max(skipna=True).values),
            ],
            "vRange": [
                json_safe_float(ds[v_var].min(skipna=True).values),
                json_safe_float(ds[v_var].max(skipna=True).values),
            ],
            "lods": lod_meta,
        }
    return {
        "bounds": bounds,
        "valueRange": [
            json_safe_float(ds[product.variable].min(skipna=True).values),
            json_safe_float(ds[product.variable].max(skipna=True).values),
        ],
        "lods": lod_meta,
    }
