"""Compare resample implementations for the LOD pyramid.

Loads a cached slice from disk and runs each candidate interpolator against the
target LOD grid sizes, timing each one and comparing output against the current
xr.interp(method='linear') baseline.

Usage:
    uv run python scripts/benchmark_resample.py
"""

from __future__ import annotations

import pickle
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import lz4.frame
import numpy as np
import xarray as xr
from PIL import Image
from scipy.interpolate import RegularGridInterpolator
from scipy.ndimage import map_coordinates, zoom

try:
    import cv2  # type: ignore

    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False

try:
    from numba import njit, prange  # type: ignore

    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


CACHE_DIR = (
    REPO / "slice_cache" / "s3:%%aodn-cloud-optimised%satellite_austemp_heatwave_8day.zarr-ssta"
)
VAR = "ssta"
CHUNK_PX = (240, 192)
N_RUNS = 3  # average per method


def load_cached_slice() -> xr.Dataset:
    files = sorted(CACHE_DIR.glob("*.pkl.lz4"))
    if not files:
        raise SystemExit(f"No cached slices found in {CACHE_DIR}")
    path = files[0]
    print(f"Loading cached slice: {path.name}")
    return cast(xr.Dataset, pickle.loads(lz4.frame.decompress(path.read_bytes())))


def lod_target_dims(src_w: int, src_h: int) -> dict[int, tuple[int, int]]:
    """Mirror Product._compute_lod_grids — finest 4 LODs, in (total_w, total_h)."""
    cw, ch = CHUNK_PX
    finest_cols = max(1, -(-src_w // cw))  # ceil
    finest_rows = max(1, -(-src_h // ch))
    import math

    depth = (
        math.floor(math.log2(max(finest_cols, finest_rows)))
        if max(finest_cols, finest_rows) > 1
        else 0
    )
    levels = []
    for k in range(depth + 1):
        scale = 2**k
        levels.append((max(1, -(-finest_cols // scale)), max(1, -(-finest_rows // scale))))
    levels.reverse()
    levels = [lvl for lvl in levels if lvl[0] >= 2 and lvl[1] >= 2]
    if not levels:
        levels = [(finest_cols, finest_rows)]
    out: dict[int, tuple[int, int]] = {}
    for i, (gc, gr) in enumerate(levels[-4:]):
        out[i + 1] = (gc * cw, gr * ch)
    return out


def run_xarray_interp(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    lon_min, lon_max = float(ds.lon.min()), float(ds.lon.max())
    lat_min, lat_max = float(ds.lat.min()), float(ds.lat.max())
    target_lons = np.linspace(lon_min, lon_max, total_w)
    target_lats = np.linspace(lat_max, lat_min, total_h)
    return ds.interp(lon=target_lons, lat=target_lats, method="linear")[VAR].values


def run_scipy_zoom(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    """scipy.ndimage.zoom — order=1 = bilinear. grid_mode=False matches xr.interp endpoints."""
    src = ds[VAR].values
    # Source may be lat increasing or decreasing; match xr.interp output orientation (lat decreasing).
    if float(ds.lat[0]) < float(ds.lat[-1]):
        src = src[::-1, :]
    src_h, src_w = src.shape
    zoom_factors = (total_h / src_h, total_w / src_w)
    # nan_to_num to avoid NaN propagation through the spline buffer (zoom handles NaN as 0).
    # We restore NaN positions from a separate mask zoom afterwards.
    valid = (~np.isnan(src)).astype(np.float32)
    src_z = np.nan_to_num(src, nan=0.0).astype(np.float32)
    out = zoom(src_z, zoom_factors, order=1, grid_mode=False, mode="nearest")
    mask = zoom(valid, zoom_factors, order=1, grid_mode=False, mode="nearest")
    out = np.where(mask > 0.5, out, np.nan)
    return out


def run_regular_grid(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    """scipy.interpolate.RegularGridInterpolator — what xarray.interp uses under the hood."""
    src_lats = ds.lat.values.astype(np.float64)
    src_lons = ds.lon.values.astype(np.float64)
    src = ds[VAR].values
    # RegularGridInterpolator requires strictly increasing axes
    if src_lats[0] > src_lats[-1]:
        src_lats = src_lats[::-1]
        src = src[::-1, :]
    interp = RegularGridInterpolator(
        (src_lats, src_lons), src, method="linear", bounds_error=False, fill_value=np.nan
    )
    target_lons = np.linspace(float(ds.lon.min()), float(ds.lon.max()), total_w)
    target_lats = np.linspace(float(ds.lat.max()), float(ds.lat.min()), total_h)
    pts = np.stack(np.broadcast_arrays(target_lats[:, None], target_lons[None, :]), axis=-1)
    return np.asarray(interp(pts))


def run_numpy_bilinear(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    """Hand-rolled bilinear interpolation in pure numpy, float32 throughout.

    Endpoint mapping matches xr.interp: out[0] = src[0], out[-1] = src[-1].
    NaN handling: any output pixel whose 4 source neighbors contain at least one
    NaN becomes NaN (matches xr.interp behaviour).
    """
    src = ds[VAR].values.astype(np.float32, copy=False)
    if float(ds.lat[0]) < float(ds.lat[-1]):
        src = src[::-1, :]
    src_h, src_w = src.shape

    # Source-space coordinate for each target pixel
    sy = np.linspace(0.0, src_h - 1, total_h, dtype=np.float32)
    sx = np.linspace(0.0, src_w - 1, total_w, dtype=np.float32)

    y0 = np.floor(sy).astype(np.int32)
    x0 = np.floor(sx).astype(np.int32)
    y1 = np.minimum(y0 + 1, src_h - 1)
    x1 = np.minimum(x0 + 1, src_w - 1)
    dy = (sy - y0).astype(np.float32)
    dx = (sx - x0).astype(np.float32)

    # Gather 4 corners as 2D arrays via outer indexing
    a = src[y0[:, None], x0[None, :]]
    b = src[y0[:, None], x1[None, :]]
    c = src[y1[:, None], x0[None, :]]
    d = src[y1[:, None], x1[None, :]]

    # Weighted bilinear blend
    wdx = dx[None, :]
    wdy = dy[:, None]
    top = a * (1 - wdx) + b * wdx
    bot = c * (1 - wdx) + d * wdx
    out = top * (1 - wdy) + bot * wdy

    # Propagate NaN: if any corner is NaN, output is NaN. np.isnan ORed across the 4.
    nan_mask = np.isnan(a) | np.isnan(b) | np.isnan(c) | np.isnan(d)
    out[nan_mask] = np.nan
    return np.asarray(out)


def run_pil_resize(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    """PIL Image.resize with BILINEAR — Pillow's C resize, float32 mode "F"."""
    src = ds[VAR].values.astype(np.float32, copy=False)
    if float(ds.lat[0]) < float(ds.lat[-1]):
        src = src[::-1, :]
    # Mode "F" = 32-bit float. NaN survives PIL but bilinear blending of NaN gives NaN,
    # which propagates naturally — matches xr.interp NaN behaviour at boundaries.
    img = Image.fromarray(src, mode="F")
    resized = img.resize((total_w, total_h), Image.Resampling.BILINEAR)
    return np.asarray(resized)


def run_pil_resize_masked(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    """PIL with two resizes: one for data (NaN→0), one for valid mask. Then propagate NaN
    where any source neighbour was NaN — matches xr.interp NaN behaviour at boundaries.
    """
    src = ds[VAR].values.astype(np.float32, copy=False)
    if float(ds.lat[0]) < float(ds.lat[-1]):
        src = src[::-1, :]
    valid = (~np.isnan(src)).astype(np.float32)
    src_clean = np.where(valid > 0, src, 0.0).astype(np.float32)
    img_data = Image.fromarray(src_clean, mode="F").resize(
        (total_w, total_h), Image.Resampling.BILINEAR
    )
    img_mask = Image.fromarray(valid, mode="F").resize(
        (total_w, total_h), Image.Resampling.BILINEAR
    )
    out = np.asarray(img_data, dtype=np.float32).copy()
    mask = np.asarray(img_mask)
    # mask < 1.0 means at least one source neighbour was NaN — match xr.interp by setting NaN.
    out[mask < 0.9999] = np.nan
    return out


def run_cv2_resize(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    """OpenCV cv2.resize with INTER_LINEAR — SIMD-optimized bilinear."""
    src = ds[VAR].values.astype(np.float32, copy=False)
    if float(ds.lat[0]) < float(ds.lat[-1]):
        src = src[::-1, :]
    # cv2.resize takes (width, height). INTER_LINEAR = bilinear.
    # NaN propagates through arithmetic — matches xr.interp behaviour at boundaries.
    return np.asarray(cv2.resize(src, (total_w, total_h), interpolation=cv2.INTER_LINEAR))


if _HAS_NUMBA:

    @njit(parallel=True, cache=True, fastmath=True)
    def _numba_bilinear(src: np.ndarray, total_h: int, total_w: int) -> np.ndarray:
        """JIT-compiled bilinear with NaN propagation. Endpoints match xr.interp."""
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

    @njit(cache=True, fastmath=True)
    def _numba_bilinear_serial(src: np.ndarray, total_h: int, total_w: int) -> np.ndarray:
        """Serial variant — useful baseline on a 2-vCPU burstable instance."""
        src_h, src_w = src.shape
        out = np.empty((total_h, total_w), dtype=np.float32)
        sy_scale = (src_h - 1.0) / (total_h - 1.0) if total_h > 1 else 0.0
        sx_scale = (src_w - 1.0) / (total_w - 1.0) if total_w > 1 else 0.0
        for i in range(total_h):
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


def run_numba_parallel(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    src = ds[VAR].values.astype(np.float32, copy=False)
    if float(ds.lat[0]) < float(ds.lat[-1]):
        src = np.ascontiguousarray(src[::-1, :])
    return np.asarray(_numba_bilinear(src, total_h, total_w))


def run_numba_serial(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    src = ds[VAR].values.astype(np.float32, copy=False)
    if float(ds.lat[0]) < float(ds.lat[-1]):
        src = np.ascontiguousarray(src[::-1, :])
    return np.asarray(_numba_bilinear_serial(src, total_h, total_w))


def run_map_coordinates(ds: xr.Dataset, total_w: int, total_h: int) -> np.ndarray:
    """scipy.ndimage.map_coordinates — explicit per-pixel coords; order=1 = bilinear."""
    src = ds[VAR].values
    if float(ds.lat[0]) < float(ds.lat[-1]):
        src = src[::-1, :]
    src_h, src_w = src.shape
    # Match xr.interp endpoint mapping: out[0] = src[0], out[-1] = src[-1]
    row_idx = np.linspace(0, src_h - 1, total_h)
    col_idx = np.linspace(0, src_w - 1, total_w)
    rows, cols = np.broadcast_arrays(row_idx[:, None], col_idx[None, :])
    valid = (~np.isnan(src)).astype(np.float32)
    src_clean = np.nan_to_num(src, nan=0.0).astype(np.float32)
    out = map_coordinates(src_clean, np.stack([rows, cols]), order=1, mode="nearest")
    mask = map_coordinates(valid, np.stack([rows, cols]), order=1, mode="nearest")
    out = np.where(mask > 0.5, out, np.nan)
    return out


def time_call(fn: Callable[..., np.ndarray], *args: object) -> tuple[float, np.ndarray]:
    times: list[float] = []
    result: np.ndarray | None = None
    for _ in range(N_RUNS):
        t0 = time.monotonic()
        result = fn(*args)
        times.append(time.monotonic() - t0)
    assert result is not None
    return min(times) * 1000, result  # best-of-N


def compare(baseline: np.ndarray, other: np.ndarray) -> dict:
    """NaN-aware diff summary."""
    both_valid = ~(np.isnan(baseline) | np.isnan(other))
    if not both_valid.any():
        return {"max_abs": 0.0, "rmse": 0.0, "valid_match_pct": 100.0}
    diff = baseline[both_valid] - other[both_valid]
    nan_b = np.isnan(baseline)
    nan_o = np.isnan(other)
    match_pct = float((nan_b == nan_o).mean()) * 100.0
    return {
        "max_abs": float(np.abs(diff).max()),
        "rmse": float(np.sqrt(np.mean(diff**2))),
        "nan_mask_match_pct": match_pct,
    }


def main() -> None:
    ds = load_cached_slice()
    src_h = ds.sizes["lat"]
    src_w = ds.sizes["lon"]
    print(f"Source dims: lat={src_h} lon={src_w}")
    print(f"Variable: {VAR}\n")

    lod_dims = lod_target_dims(src_w, src_h)

    methods: list[tuple[str, Callable[..., np.ndarray]]] = [
        ("xr.interp (baseline)", run_xarray_interp),
        ("scipy.ndimage.zoom", run_scipy_zoom),
        ("scipy.RegularGridInterp", run_regular_grid),
        ("scipy.map_coordinates", run_map_coordinates),
        ("custom numpy bilinear", run_numpy_bilinear),
        ("PIL Image.resize", run_pil_resize),
        ("PIL + NaN mask", run_pil_resize_masked),
    ]
    if _HAS_CV2:
        methods.append(("cv2.resize INTER_LINEAR", run_cv2_resize))
    else:
        print("(cv2 not installed; skipping OpenCV. Install with: uv add opencv-python-headless)\n")
    if _HAS_NUMBA:
        methods.append(("numba parallel (2 thr)", run_numba_parallel))
        methods.append(("numba serial", run_numba_serial))
        # Warm the JIT outside the timed loop — first call is dominated by compilation.
        print("Warming numba JIT (first call compiles)...")
        sample = ds[VAR].values.astype(np.float32, copy=False)[:16, :16]
        _numba_bilinear(sample, 32, 32)
        _numba_bilinear_serial(sample, 32, 32)
        print("Warmup complete.\n")
    else:
        print("(numba not installed; skipping. Install with: uv add numba)\n")

    for lod, (total_w, total_h) in sorted(lod_dims.items()):
        print(f"=== LOD {lod}  target {total_w}×{total_h} ===")
        baseline = None
        results: list[tuple[str, float, dict[str, float], float]] = []
        for name, fn in methods:
            try:
                ms, out = time_call(fn, ds, total_w, total_h)
            except Exception as e:
                print(f"  {name:<28}  FAILED  {e}")
                continue
            if baseline is None:
                baseline = out
                diff = {"max_abs": 0.0, "rmse": 0.0, "nan_mask_match_pct": 100.0}
            else:
                diff = compare(baseline, out)
            speedup = (results[0][1] / ms) if results else 1.0
            results.append((name, ms, diff, speedup))

        print(
            f"  {'method':<28} {'ms':>8} {'speedup':>8} {'max_abs':>10} {'rmse':>10} {'nan_match':>10}"
        )
        for name, ms, diff, speedup in results:
            print(
                f"  {name:<28} {ms:>8.1f} {speedup:>8.2f}x "
                f"{diff['max_abs']:>10.4g} {diff['rmse']:>10.4g} "
                f"{diff['nan_mask_match_pct']:>9.2f}%"
            )
        print()


if __name__ == "__main__":
    main()
