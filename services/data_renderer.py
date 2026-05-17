import logging
import math
import os
from collections.abc import Callable
from io import BytesIO

import numpy as np
import xarray as xr
from cachetools import LRUCache
from PIL import Image

from constants import LOD, Product
from utils.geo import dataset_bounds, json_safe_float
from utils.memoizer import Memoizer

logger = logging.getLogger(__name__)


_PROCESSED_CACHE_SIZE = int(os.environ.get("PROCESSED_CACHE_SIZE", 50))
_processed_cache: LRUCache = LRUCache(maxsize=_PROCESSED_CACHE_SIZE)
_processed_memo: Memoizer = Memoizer(_processed_cache)


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
    # All-NaN slice: min/max return NaN. Fall back to a benign range so _normalize
    # produces well-defined zeros; the ocean mask will mark every pixel transparent.
    if math.isnan(lo) or math.isnan(hi):
        return (0.0, 1.0)
    return (lo, hi) if hi != lo else (lo, lo + 1.0)


def _compute_processed(
    product: Product, ds: xr.Dataset, lod: int
) -> tuple[list[np.ndarray], np.ndarray]:
    """Resample every product variable to the LOD grid and normalise.

    Returns ``(normalised, ocean)`` where:
      * ``normalised`` is one array per variable in ``product.variables`` order.
        Scalar products (1 variable) get one ``uint32`` array normalised across
        24 bits (R/G/B packed in render_tile). Multi-variable products (e.g. UV
        currents, 2 variables) get one ``uint8`` array per variable, normalised
        across 8 bits — each variable lives in its own channel.
      * ``ocean`` is ``uint8`` (0/1), 1 where *every* variable has a valid value.
        For multi-variable products this prevents one channel encoding a sentinel
        zero while the mask claims valid data.
    """
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]
    variables = product.variables

    ds_r = _resample_to_grid(ds[variables], total_w, total_h)
    raw = [ds_r[v].values.squeeze() for v in variables]

    invalid = np.zeros(raw[0].shape, dtype=bool)
    for arr in raw:
        invalid |= np.isnan(arr)
    ocean = (~invalid).astype(np.uint8)

    # Scalar: pack one value across 3 bytes (R/G/B) for sub-percent precision over the
    # data range. Multi-variable: one byte per channel — precision drops to ~0.4%, but
    # the frontend shader needs each channel independently addressable.
    out_max = 16777215 if len(variables) == 1 else 255
    normalised = [
        _normalize(r, *_var_range(ds, v), out_max) for r, v in zip(raw, variables, strict=True)
    ]
    return normalised, ocean


def _get_processed(
    product: Product, load_ds: Callable[[], xr.Dataset], lod: int, date: str
) -> tuple[list[np.ndarray], np.ndarray]:
    """load_ds only called once per (product, date) when the processed grid is not cached yet."""
    key = (product.source_path, date, tuple(product.variables), lod)

    def factory() -> tuple[list[np.ndarray], np.ndarray]:
        return _compute_processed(product, load_ds(), lod)

    return _processed_memo.get_or_compute(key, factory)


def evict_processed_cache(product: Product) -> None:
    removed = _processed_memo.evict_matching(lambda k: k[0] == product.source_path)
    if removed:
        logger.info("Processed cache evicted %d entry/entries for: %s", removed, product.id)


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
    normalised, ocean = _get_processed(product, load_ds, lod, date)
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    def chunk_of(arr: np.ndarray) -> np.ndarray:
        return _extract_chunk(arr, cx, cy, total_w, total_h, product.chunk_px, product.padding)

    chunks = [chunk_of(arr) for arr in normalised]
    chunk_m = chunk_of(ocean)
    h, w = chunk_m.shape
    img = np.zeros((h, w, 4), dtype=np.uint8)

    if len(chunks) == 1:
        # Scalar: one 24-bit value spread across R/G/B; alpha carries the ocean mask.
        # Force RGB to 0 for non-ocean pixels so partial PNG decoders still see a clean
        # transparent boundary even if they ignore alpha.
        val = chunks[0]
        img[:, :, 0] = (val >> 16) & 0xFF
        img[:, :, 1] = (val >> 8) & 0xFF
        img[:, :, 2] = val & 0xFF
        img[:, :, 3] = chunk_m * 255
        img[chunk_m == 0, :3] = 0
    else:
        # Multi-variable (e.g. UV currents): each variable in its own channel,
        # mask in the next channel, alpha kept opaque so the shader can use B as data.
        img[:, :, 0] = chunks[0]
        img[:, :, 1] = chunks[1]
        img[:, :, 2] = chunk_m * 255
        img[:, :, 3] = 255

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
