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

import threading

import numpy as np
import xarray as xr

from app.services.colormap.categorical import is_categorical_variable

# Serialises entry into the parallel=True kernels below: only one parallel region
# may be open in the process at a time.
#
# Parallel region: one in-flight execution of a prange loop — it opens when the
# loop starts (the threading layer wakes its workers and splits the iterations
# across cores) and closes when all workers join. The hazard here is two regions
# being open *simultaneously*, not two threads touching the same data.
#
# The bug: Numba drives a parallel region through its threading layer. With neither
# TBB nor OpenMP installed it falls back to `workqueue`, which is NOT threadsafe —
# two threads entering parallel regions at once corrupt its shared scheduler state,
# either silently garbling output buffers (a tile returns with a full-range alpha
# channel instead of a 0/255 ocean mask) or aborting the process on builds with the
# concurrency guard. Our sync tile handlers run in the AnyIO thread pool, so a burst
# of tile requests triggers exactly this.
#
# Why data uniqueness doesn't save us (a natural point of confusion): each request
# is unique (product/date/LOD) with its own `src` input and freshly-allocated `out`,
# so there is no race on *our* data. But the corrupted state isn't ours — it's the
# threading layer's process-global scheduler (the single workqueue), shared by every
# parallel region regardless of the data it touches.
#
# Why a lock, not TBB/OpenMP: forcing a threadsafe layer would make correctness
# depend on an unpinned native lib being present on every deploy — a silent fallback
# to workqueue brings the bug back with no error. The lock is platform-independent,
# and its cost is negligible: a region still uses every core for its own prange loop
# (we only forbid two regions overlapping), and the guarded section (resample +
# normalize) is a few ms, dwarfed by the S3 slice fetch.
# Threading-layer docs: https://numba.readthedocs.io/en/stable/user/threading-layer.html
#
# Scope: one module-level lock guards *all four* parallel kernels (both resample +
# both normalize) — any two overlapping regions are unsafe, so a thread in
# _numba_bilinear must also block one entering _numba_normalize_uint32. It is held
# per *kernel call*, not per request: the call sites wrap only the kernel(...)
# invocation, so prep work (.astype/.squeeze/flip) runs unlocked and a multi-variable
# resample acquires/releases once per variable. A waiting thread blocks only for the
# current prange execution (a few ms), never a peer request's whole pipeline.
_PARALLEL_KERNEL_LOCK = threading.Lock()


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

    @njit(parallel=True, cache=True, fastmath=True)
    def _numba_nearest(src: np.ndarray, total_h: int, total_w: int) -> np.ndarray:
        """JIT nearest-neighbour resample on the same linspace(0, src-1, total)
        mapping as `_numba_bilinear`, but picking the single closest source cell
        instead of blending four.

        Required for categorical (CF flag_values) variables: bilinear would average
        adjacent integer codes into fabricated in-between categories, and coarser
        LODs compound it. Nearest preserves the exact code (and NaN, since it copies
        the source value verbatim). Ties (`sy` exactly on .5) round up.
        """
        src_h, src_w = src.shape
        out = np.empty((total_h, total_w), dtype=np.float32)
        sy_scale = (src_h - 1.0) / (total_h - 1.0) if total_h > 1 else 0.0
        sx_scale = (src_w - 1.0) / (total_w - 1.0) if total_w > 1 else 0.0
        for i in prange(total_h):
            y = int(i * sy_scale + 0.5)
            if y >= src_h:
                y = src_h - 1
            for j in range(total_w):
                x = int(j * sx_scale + 0.5)
                if x >= src_w:
                    x = src_w - 1
                out[i, j] = src[y, x]
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
    print("numba unavailable; falling back to xr.interp (~5× slower on Intel)")


def resample_variables_to_grid(
    ds: xr.Dataset, variables: list[str], total_w: int, total_h: int
) -> list[np.ndarray]:
    """Resample each named variable to a (total_h, total_w) grid.

    Continuous variables are bilinear-resampled; categorical variables (CF
    flag_values) are nearest-resampled so their discrete integer codes are never
    blended into fabricated categories. Output pixel positions follow
    np.linspace(0, src-1, total) on both axes — the same mapping the WebGL shader
    assumes (see docs/technical.md §5.6). NaN propagates where any contributing
    source neighbour is NaN, matching xr.interp.

    Returns a list of float32 ndarrays in the same order as ``variables``, each
    oriented north→south.
    """
    # Orient source north→south so index-based resampling matches the shader's lat mapping.
    flip = float(ds.lat[0]) < float(ds.lat[-1])

    if _HAS_NUMBA:
        out: list[np.ndarray] = []
        for v in variables:
            arr = ds[v].values.astype(np.float32, copy=False).squeeze()
            if flip:
                arr = np.ascontiguousarray(arr[::-1, :])
            kernel = _numba_nearest if is_categorical_variable(ds[v].attrs) else _numba_bilinear
            with _PARALLEL_KERNEL_LOCK:
                out.append(kernel(arr, total_h, total_w))
        return out

    # Fallback: xarray's interp on the same linspace mapping, per-variable method.
    lon_min = float(ds.lon.min())
    lon_max = float(ds.lon.max())
    lat_min = float(ds.lat.min())
    lat_max = float(ds.lat.max())
    target_lons = np.linspace(lon_min, lon_max, total_w)
    target_lats = np.linspace(lat_max, lat_min, total_h)  # north → south
    out = []
    for v in variables:
        method = "nearest" if is_categorical_variable(ds[v].attrs) else "linear"
        r = ds[v].interp(lon=target_lons, lat=target_lats, method=method)
        out.append(r.values.squeeze().astype(np.float32, copy=False))
    return out


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
        with _PARALLEL_KERNEL_LOCK:
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
    ds = xr.Dataset(
        {"v": (("lat", "lon"), np.zeros((16, 16), dtype=np.float32))},
        coords={"lat": np.linspace(1.0, 0.0, 16), "lon": np.linspace(0.0, 1.0, 16)},
    )
    resample_variables_to_grid(ds, ["v"], 32, 32)
    if _HAS_NUMBA:
        # Called once at startup before any request is served, so these direct
        # kernel calls don't need _PARALLEL_KERNEL_LOCK — nothing else can be in a
        # parallel region yet.
        sample = np.zeros((32, 32), dtype=np.float32)
        _numba_nearest(sample, 32, 32)
        _numba_normalize_uint32(sample, 0.0, 1.0, 16777215)
        _numba_normalize_uint8(sample, 0.0, 1.0, 255)
