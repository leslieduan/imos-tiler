import logging
import math
import os
import time
from collections.abc import Callable

import numpy as np
import xarray as xr
from cachetools import TTLCache

from app.constants import LOD
from app.domain.product import Product
from app.utils.geo import json_safe_float
from app.utils.image import encode_rgba
from app.utils.memoizer import Memoizer

logger = logging.getLogger(__name__)


try:
    from numba import njit, prange

    _HAS_NUMBA = True

    # fastmath=True (all flags) is safe here even though `nnan` claims "no NaN":
    # NaN propagates through hardware FP arithmetic regardless of the compile-time
    # nnan flag (which only enables removing explicit isnan checks, not changing
    # FP op semantics). Verified by the resample benchmark (100% nan_match vs
    # xr.interp). The explicit isnan check below is dead code under fastmath but
    # left for readability and as a guard if fastmath is ever disabled.
    @njit(parallel=True, cache=True, fastmath=True)
    def _numba_bilinear(src: np.ndarray, total_h: int, total_w: int) -> np.ndarray:
        """JIT-compiled bilinear with NaN propagation. Output positions match
        np.linspace(0, src-1, total) on both axes — the same mapping xr.interp
        produces, and the same mapping the WebGL shader assumes (see docs/technical.md §5.6).

        Inputs:
          src: float32, shape (src_h, src_w), oriented north→south.
          total_h, total_w: target dims.

        Output: float32 (total_h, total_w). NaN where any of the 4 source neighbours is NaN.
        """
        src_h, src_w = src.shape
        out = np.empty((total_h, total_w), dtype=np.float32)
        sy_scale = (src_h - 1.0) / (total_h - 1.0) if total_h > 1 else 0.0
        sx_scale = (src_w - 1.0) / (total_w - 1.0) if total_w > 1 else 0.0
        for i in prange(total_h):
            sy = i * sy_scale
            y0 = int(sy)
            y1 = y0 + 1 if y0 + 1 < src_h else src_h - 1
            dy = sy - y0
            for j in range(total_w):
                sx = j * sx_scale
                x0 = int(sx)
                x1 = x0 + 1 if x0 + 1 < src_w else src_w - 1
                dx = sx - x0
                a = src[y0, x0]
                b = src[y0, x1]
                c = src[y1, x0]
                d = src[y1, x1]
                if np.isnan(a) or np.isnan(b) or np.isnan(c) or np.isnan(d):
                    out[i, j] = np.nan
                else:
                    top = a * (1.0 - dx) + b * dx
                    bot = c * (1.0 - dx) + d * dx
                    out[i, j] = top * (1.0 - dy) + bot * dy
        return out

    # Selective fastmath (no 'nnan') so np.isnan() works correctly inside the
    # kernel. Folds the NaN-mask scan into the normalize pass — one traversal
    # produces both the normalized output and the per-pixel valid mask, which
    # is significantly faster than a separate isnan kernel + normalize kernel
    # (two full grid reads vs one).
    @njit(
        parallel=True,
        cache=True,
        fastmath={"nsz", "arcp", "contract", "afn", "reassoc"},
    )
    def _numba_normalize_uint32(
        arr: np.ndarray, lo: float, hi: float, out_max: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Normalize float32 → uint32 in one pass, also producing the per-pixel
        valid mask (1 where non-NaN, 0 where NaN).
        """
        h, w = arr.shape
        out = np.empty((h, w), dtype=np.uint32)
        valid = np.empty((h, w), dtype=np.uint8)
        span = hi - lo if hi != lo else 1.0
        scale = (1.0 / span) * out_max
        out_max_f = float(out_max)
        for i in prange(h):
            for j in range(w):
                v = arr[i, j]
                if np.isnan(v):
                    out[i, j] = np.uint32(0)
                    valid[i, j] = np.uint8(0)
                else:
                    val = (v - lo) * scale
                    if val < 0.0:
                        val = 0.0
                    elif val > out_max_f:
                        val = out_max_f
                    out[i, j] = np.uint32(val)
                    valid[i, j] = np.uint8(1)
        return out, valid

    @njit(
        parallel=True,
        cache=True,
        fastmath={"nsz", "arcp", "contract", "afn", "reassoc"},
    )
    def _numba_normalize_uint8(
        arr: np.ndarray, lo: float, hi: float, out_max: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """uint8 specialisation of _numba_normalize_uint32 for multi-variable products."""
        h, w = arr.shape
        out = np.empty((h, w), dtype=np.uint8)
        valid = np.empty((h, w), dtype=np.uint8)
        span = hi - lo if hi != lo else 1.0
        scale = (1.0 / span) * out_max
        out_max_f = float(out_max)
        for i in prange(h):
            for j in range(w):
                v = arr[i, j]
                if np.isnan(v):
                    out[i, j] = np.uint8(0)
                    valid[i, j] = np.uint8(0)
                else:
                    val = (v - lo) * scale
                    if val < 0.0:
                        val = 0.0
                    elif val > out_max_f:
                        val = out_max_f
                    out[i, j] = np.uint8(val)
                    valid[i, j] = np.uint8(1)
        return out, valid

except (
    ImportError
):  # pragma: no cover — numba is a hard dep; this guards against broken install only
    _HAS_NUMBA = False
    logger.warning("numba unavailable; falling back to xr.interp (~5× slower on Intel)")


_PROCESSED_CACHE_SIZE = int(os.environ.get("PROCESSED_CACHE_SIZE", 50))
# L1 entries serve the tile-burst for one (product, date, LOD); same idle-RAM
# argument as L2 (see services/loader.py). TTL is insertion-based, so a
# stationary session >TTL incurs one re-resample on the next request — ~10-50 ms,
# negligible vs the steady-RAM savings during idle periods.
_PROCESSED_CACHE_TTL = int(os.environ.get("PROCESSED_CACHE_TTL_SECONDS", 600))
_processed_cache: TTLCache = TTLCache(maxsize=_PROCESSED_CACHE_SIZE, ttl=_PROCESSED_CACHE_TTL)
_processed_memo: Memoizer = Memoizer(_processed_cache)


def warmup_resample() -> None:
    """Prime the numba JIT (and scipy/BLAS in the fallback path) so the first real
    tile request doesn't pay one-time init overhead. Synchronous; intended to be
    called once during startup.
    """
    t0 = time.monotonic()
    ds = xr.Dataset(
        {"v": (("lat", "lon"), np.zeros((16, 16), dtype=np.float32))},
        coords={"lat": np.linspace(1.0, 0.0, 16), "lon": np.linspace(0.0, 1.0, 16)},
    )
    _resample_variables_to_grid(ds, ["v"], 32, 32)
    if _HAS_NUMBA:
        sample = np.zeros((32, 32), dtype=np.float32)
        _numba_normalize_uint32(sample, 0.0, 1.0, 16777215)
        _numba_normalize_uint8(sample, 0.0, 1.0, 255)
    logger.debug(
        "[timing] resample warmup",
        extra={
            "ms": round((time.monotonic() - t0) * 1000, 1),
            "backend": "numba" if _HAS_NUMBA else "xr.interp",
        },
    )


def _resample_variables_to_grid(
    ds: xr.Dataset, variables: list[str], total_w: int, total_h: int
) -> list[np.ndarray]:
    """Bilinear-resample each named variable to a (total_h, total_w) grid.

    Output pixel positions follow np.linspace(0, src-1, total) on both axes — the
    same mapping the WebGL shader assumes (see docs/technical.md §5.6). NaN propagates
    where any of the 4 source neighbours is NaN, matching xr.interp(method='linear').

    Returns a list of float32 ndarrays in the same order as ``variables``, each
    oriented north→south.
    """
    # Orient source north→south so index-based bilinear matches the shader's lat mapping.
    flip = float(ds.lat[0]) < float(ds.lat[-1])

    if _HAS_NUMBA:
        out: list[np.ndarray] = []
        for v in variables:
            arr = ds[v].values.astype(np.float32, copy=False).squeeze()
            if flip:
                arr = np.ascontiguousarray(arr[::-1, :])
            out.append(_numba_bilinear(arr, total_h, total_w))
        return out

    # Fallback: xarray's bilinear interp on the same linspace mapping.
    lon_min = float(ds.lon.min())
    lon_max = float(ds.lon.max())
    lat_min = float(ds.lat.min())
    lat_max = float(ds.lat.max())
    target_lons = np.linspace(lon_min, lon_max, total_w)
    target_lats = np.linspace(lat_max, lat_min, total_h)  # north → south
    ds_r = ds[variables].interp(lon=target_lons, lat=target_lats, method="linear")
    return [ds_r[v].values.squeeze().astype(np.float32, copy=False) for v in variables]


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

    t0 = time.monotonic()
    raw = _resample_variables_to_grid(ds, variables, total_w, total_h)
    resample_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    # Scalar: pack one value across 3 bytes (R/G/B) for sub-percent precision over the
    # data range. Multi-variable: one byte per channel — precision drops to ~0.4%, but
    # the frontend shader needs each channel independently addressable.
    out_max = 16777215 if len(variables) == 1 else 255
    normalised: list[np.ndarray] = []
    valid_masks: list[np.ndarray] = []
    for r, v in zip(raw, variables, strict=True):
        lo, hi = _var_range(ds, v)
        if _HAS_NUMBA:
            if out_max > 255:
                norm, valid = _numba_normalize_uint32(r, lo, hi, out_max)
            else:
                norm, valid = _numba_normalize_uint8(r, lo, hi, out_max)
        else:
            norm = _normalize(r, lo, hi, out_max)
            valid = (~np.isnan(r)).astype(np.uint8)
        normalised.append(norm)
        valid_masks.append(valid)

    # ocean = AND of per-variable valid masks (1 where every variable is non-NaN).
    if len(valid_masks) == 1:
        ocean = valid_masks[0]
    else:
        ocean = valid_masks[0].copy()
        for vm in valid_masks[1:]:
            ocean &= vm
    normalize_ms = (time.monotonic() - t0) * 1000

    logger.debug(
        "[timing] processed grid built",
        extra={
            "product_id": product.id,
            "lod": lod,
            "grid_px": f"{total_w}x{total_h}",
            "resample_ms": round(resample_ms, 1),
            "normalize_ms": round(normalize_ms, 1),
        },
    )
    return normalised, ocean


def _get_processed(
    product: Product, load_ds: Callable[[], xr.Dataset], lod: int, date: str
) -> tuple[list[np.ndarray], np.ndarray]:
    """load_ds only called once per (product, date) when the processed grid is not cached yet."""
    key = (product.source_path, date, tuple(product.variables), lod)

    def factory() -> tuple[list[np.ndarray], np.ndarray]:
        return _compute_processed(product, load_ds(), lod)

    return _processed_memo.get_or_compute(key, factory)


def processed_memo_stats() -> dict:
    """In-flight + LRU stats for the L1 processed-grid memoizer. Used by /admin/cache."""
    return {
        **_processed_memo.stats(),
        "cache_size": len(_processed_cache),
        "cache_max": _processed_cache.maxsize,
    }


def evict_processed_cache(product: Product) -> None:
    vars_tuple = tuple(product.variables)
    removed = _processed_memo.evict_matching(
        # Identify product by source_path and variables;
        lambda k: k[0] == product.source_path and k[2] == vars_tuple
    )
    if removed:
        logger.info(
            "Processed cache evicted for product",
            extra={"product_id": product.id, "entries_removed": removed},
        )


def clear_processed_cache() -> int:
    """Drop every entry in the L1 processed-grid cache. Returns count removed."""
    removed = _processed_memo.evict_matching(lambda _: True)
    if removed:
        logger.info("Processed cache cleared", extra={"entries_removed": removed})
    return removed


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


def render_tile(
    product: Product, load_ds: Callable[[], xr.Dataset], lod: int, cx: int, cy: int, date: str
) -> bytes:
    t_total = time.monotonic()
    key = (product.source_path, date, tuple(product.variables), lod)
    l1_hit = key in _processed_cache

    t0 = time.monotonic()
    normalised, ocean = _get_processed(product, load_ds, lod, date)
    get_processed_ms = (time.monotonic() - t0) * 1000

    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    t0 = time.monotonic()

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

    pack_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    png = encode_rgba(img)
    encode_ms = (time.monotonic() - t0) * 1000

    logger.debug(
        "[timing] tile rendered",
        extra={
            "product_id": product.id,
            "date": date,
            "lod": lod,
            "tile": f"{cx}/{cy}",
            "l1_hit": l1_hit,
            "get_processed_ms": round(get_processed_ms, 1),
            "pack_ms": round(pack_ms, 1),
            "encode_ms": round(encode_ms, 1),
            "total_ms": round((time.monotonic() - t_total) * 1000, 1),
        },
    )
    return png


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
