"""
Zarr tile renderer.

Uses the same processed grid cache pattern as renderer.py:
  - _get_zarr_processed ressamples the full LOD grid once per (date, lod) and caches the
    final numpy arrays — identical to the NetCDF processed cache.
  - The Zarr slice is already fully in RAM (computed by zarr_loader), so the resample
    is pure CPU with no S3 I/O.
  - _extract_chunk and _to_png_bytes are imported from renderer.py to avoid duplication.
"""

import threading

import numpy as np
import xarray as xr
from cachetools import LRUCache

from constants import LOD_ZOOM_THRESHOLDS, Product
from services.netcdf_renderer import _extract_chunk, _resample_to_grid, _to_png_bytes

# Caches the full resampled grid arrays for a (ds, lod) pair so that all tile
# requests for the same date+LOD share one resample instead of each repeating it.
# Key is (id(ds), lod): each product's ds is a distinct object from _slice_cache, so
# id(ds) implicitly encodes the product. id reuse is impossible because _slice_cache
# holds a strong reference to every ds, preventing GC for the cache's lifetime.
_zarr_processed_cache: LRUCache = LRUCache(maxsize=20)
_zarr_processed_inflight: dict = {}
_zarr_processed_lock = threading.Lock()


def _compute_scalar(product: Product, ds: xr.Dataset, lod: int) -> tuple[np.ndarray, np.ndarray]:
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


def _compute_uv(product: Product, ds: xr.Dataset, lod: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
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


def _get_zarr_processed(product: Product, ds: xr.Dataset, lod: int) -> tuple:
    key = (id(ds), lod)

    while True:
        with _zarr_processed_lock:
            cached = _zarr_processed_cache.get(key)
            if cached is not None:
                return cached
            if key not in _zarr_processed_inflight:
                event = threading.Event()
                _zarr_processed_inflight[key] = event
                break
            event = _zarr_processed_inflight[key]
        event.wait()

    try:
        result = (
            _compute_uv(product, ds, lod)
            if isinstance(product.variable, list)
            else _compute_scalar(product, ds, lod)
        )
        with _zarr_processed_lock:
            _zarr_processed_cache[key] = result
        return result
    finally:
        with _zarr_processed_lock:
            del _zarr_processed_inflight[key]
        event.set()


def render_zarr_tile(product: Product, ds: xr.Dataset, lod: int, cx: int, cy: int) -> bytes:
    grid_cols, grid_rows = product.lod_grids[lod]
    total_w = grid_cols * product.chunk_px[0]
    total_h = grid_rows * product.chunk_px[1]

    if isinstance(product.variable, list):
        u_norm, v_norm, ocean = _get_zarr_processed(product, ds, lod)
        chunk_u = _extract_chunk(u_norm, cx, cy, total_w, total_h, product.chunk_px, product.padding)
        chunk_v = _extract_chunk(v_norm, cx, cy, total_w, total_h, product.chunk_px, product.padding)
        chunk_m = _extract_chunk(ocean,  cx, cy, total_w, total_h, product.chunk_px, product.padding)
        h, w = chunk_u.shape
        img = np.zeros((h, w, 4), dtype=np.uint8)
        img[:, :, 0] = chunk_u
        img[:, :, 1] = chunk_v
        img[:, :, 2] = chunk_m * 255
        img[:, :, 3] = 255
    else:
        val_24, ocean = _get_zarr_processed(product, ds, lod)
        chunk_24 = _extract_chunk(val_24, cx, cy, total_w, total_h, product.chunk_px, product.padding)
        chunk_m  = _extract_chunk(ocean,  cx, cy, total_w, total_h, product.chunk_px, product.padding)
        h, w = chunk_24.shape
        img = np.zeros((h, w, 4), dtype=np.uint8)
        img[:, :, 0] = (chunk_24 >> 16) & 0xFF
        img[:, :, 1] = (chunk_24 >> 8)  & 0xFF
        img[:, :, 2] =  chunk_24         & 0xFF
        img[:, :, 3] = chunk_m * 255
        img[chunk_m == 0, :3] = 0

    return _to_png_bytes(img)


def render_zarr_manifest(product: Product, ds: xr.Dataset) -> dict:
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
