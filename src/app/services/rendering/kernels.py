"""Numba JIT kernels and the xarray fallback for resample + normalize.

Hot path for every cold-L1 data tile. Two pieces:
  * ``_resample_variables_to_grid`` — bilinear-resamples one or more variables
    to the LOD grid the shader expects (see docs/technical.md §5.6 — output
    pixel positions match np.linspace(0, src-1, total) on both axes).
  * ``_normalize_uint8`` / ``_normalize_uint32`` (numba) or ``_normalize_fallback``
    (xarray): convert float32 → uint with the per-pixel valid mask folded in.

``warmup_resample`` primes the JIT and BLAS at startup so the first real tile
request doesn't pay the one-time init cost.
"""

import logging
import time

import numpy as np
import xarray as xr

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


def resample_variables_to_grid(
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


def normalize_fallback(arr: np.ndarray, lo: float, hi: float, out_max: int) -> np.ndarray:
    """Normalize arr to [0, out_max], replacing NaN with 0. Returns uint8 or uint32.

    Used only when numba is unavailable; the numba kernels above are 5× faster on Intel.
    """
    span = hi - lo if hi != lo else 1.0
    result = np.clip((np.nan_to_num(arr, nan=0.0) - lo) / span * out_max, 0, out_max)
    return result.astype(np.uint32 if out_max > 255 else np.uint8)


def normalize(arr: np.ndarray, lo: float, hi: float, out_max: int) -> tuple[np.ndarray, np.ndarray]:
    """Normalize float32 → uint (uint32 if out_max > 255 else uint8) + per-pixel valid mask.

    Single dispatch point so the caller (rendering/data_tiles.py) doesn't repeat
    the numba/fallback branch and out_max → dtype selection.
    """
    if _HAS_NUMBA:
        if out_max > 255:
            return _numba_normalize_uint32(arr, lo, hi, out_max)
        return _numba_normalize_uint8(arr, lo, hi, out_max)
    norm = normalize_fallback(arr, lo, hi, out_max)
    valid = (~np.isnan(arr)).astype(np.uint8)
    return norm, valid


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
    resample_variables_to_grid(ds, ["v"], 32, 32)
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
