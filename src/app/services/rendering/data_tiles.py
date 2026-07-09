"""Data-tile rendering pipeline.

End-to-end path for a single ``/data_tiles/{product}/{date}/{z}/{x}/{y}.png``
request: pull the processed grid for (product, date, lod) from L1 (or compute
it via the kernels), extract the chunk for (x, y) with edge padding, pack into
RGBA, encode PNG.

Scalar products use a 24-bit normalised uint spread across R/G/B (alpha carries
the ocean mask). Multi-variable products (e.g. UV currents) put one variable in
each of R/G with the mask in B (alpha stays opaque so the shader can use B as
data).
"""

import math
from collections.abc import Callable

import numpy as np
import xarray as xr

from app.services.caching.deduper import Deduper
from app.services.caching.processed_cache import processed_memo
from app.services.product.product import Product
from app.services.rendering.kernels import normalize, resample_variables_to_grid
from app.services.rendering.masks import (
    inpaint_nearest,
    land_mask_for_grid,
)
from app.utils.image import encode_rgba

# Always in-process, independent of CACHE_BACKEND — see Deduper's docstring
# for why this matters even (especially) under CACHE_BACKEND=none.
_processed_dedup = Deduper()


def _var_range(ds: xr.Dataset, var: str) -> tuple[float, float]:
    lo = float(ds[var].min(skipna=True).values)
    hi = float(ds[var].max(skipna=True).values)
    # All-NaN slice: min/max return NaN. Fall back to a benign range so the
    # normalize path produces well-defined zeros; the ocean mask will mark every
    # pixel transparent.
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

    raw = resample_variables_to_grid(ds, variables, total_w, total_h)
    # Sparse products (e.g. GSLA): extend valid data toward the coast before
    # normalising, so the filled cells register as valid in the per-variable mask.
    if product.coastal_fill is not None:
        raw = [inpaint_nearest(r, product.coastal_fill.max_dist_px) for r in raw]

    # Scalar: pack one value across 3 bytes (R/G/B) for sub-percent precision over the
    # data range. Multi-variable: one byte per channel — precision drops to ~0.4%, but
    # the frontend shader needs each channel independently addressable.
    out_max = 16777215 if len(variables) == 1 else 255
    normalised: list[np.ndarray] = []
    valid_masks: list[np.ndarray] = []
    for r, v in zip(raw, variables, strict=True):
        lo, hi = _var_range(ds, v)
        norm, valid = normalize(r, lo, hi, out_max)
        normalised.append(norm)
        valid_masks.append(valid)

    # ocean = AND of per-variable valid masks (1 where every variable is non-NaN).
    if len(valid_masks) == 1:
        ocean = valid_masks[0]
    else:
        ocean = valid_masks[0].copy()
        for vm in valid_masks[1:]:
            ocean &= vm

    # Anomalous values outside the model's valid ocean domain (Product.ocean_masked)
    # are already nulled on the raw slice (masks.apply_ocean_mask), so they arrive
    # here as NaN and fall out of the per-variable valid masks above — nothing to do.
    if product.coastal_fill is not None:
        lon_min, lon_max = float(ds.lon.min()), float(ds.lon.max())
        lat_min, lat_max = float(ds.lat.min()), float(ds.lat.max())
        # Cut the coastal fill (and any data that bled over land) back off using
        # the real Natural Earth coastline, so we never paint fabricated values
        # onto land.
        land = land_mask_for_grid(lon_min, lon_max, lat_min, lat_max, total_w, total_h)
        ocean = ocean & ~land

    return normalised, ocean


def _get_processed(
    product: Product, load_ds: Callable[[], xr.Dataset], lod: int, date: str
) -> tuple[list[np.ndarray], np.ndarray]:
    """load_ds only called once per (product, date) when the processed grid is not cached yet.

    Concurrent identical requests always share one compute in-process via
    ``_processed_dedup`` (independent of ``CACHE_BACKEND``); when
    ``CACHE_BACKEND=redis``, ``processed_memo`` additionally coalesces across
    instances and caches the result.
    """
    key = (product.source_path, date, tuple(product.variables), lod)

    def factory() -> tuple[list[np.ndarray], np.ndarray]:
        return _compute_processed(product, load_ds(), lod)

    def compute() -> tuple[list[np.ndarray], np.ndarray]:
        return processed_memo.get_or_compute(key, factory)

    return _processed_dedup.dedupe(key, compute)


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

    return encode_rgba(img)
