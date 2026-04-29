"""
Zarr tile renderer — per-tile geographic bbox approach.

Unlike the NetCDF renderer which resamples the full LOD grid once and slices,
this renderer reads only the Zarr chunks covering the tile's geographic extent,
then resamples that small region to tile pixel size.

Flow per tile request:
  1. Compute tile's lat/lon bbox (including padding) from (lod, cx, cy)
  2. ds.sel(lat=..., lon=...) — lazy spatial selection, fetches only needed Zarr chunks
  3. ds.interp()              — resample small region (~tens of source points → 242×194 px)
  4. PNG encode
"""

from io import BytesIO

import numpy as np
import xarray as xr
from PIL import Image

from constants import Product


def _tile_extent(
    product: Product,
    lod: int,
    cx: int,
    cy: int,
    ds: xr.Dataset,
) -> tuple[float, float, float, float]:
    """
    Return (lat_min, lat_max, lon_min, lon_max) for the tile including padding.
    cy=0 is the northernmost tile — lat_max decreases as cy increases.
    """
    lat_min_g = float(ds.lat.min())
    lat_max_g = float(ds.lat.max())
    lon_min_g = float(ds.lon.min())
    lon_max_g = float(ds.lon.max())

    grid_cols, grid_rows = product.lod_grids[lod]
    cw, ch = product.chunk_px

    lon_per_chunk = (lon_max_g - lon_min_g) / grid_cols
    lat_per_chunk = (lat_max_g - lat_min_g) / grid_rows

    tile_lat_max = lat_max_g - cy * lat_per_chunk
    tile_lat_min = lat_max_g - (cy + 1) * lat_per_chunk
    tile_lon_min = lon_min_g + cx * lon_per_chunk
    tile_lon_max = lon_min_g + (cx + 1) * lon_per_chunk

    pad_lon = product.padding * lon_per_chunk / cw
    pad_lat = product.padding * lat_per_chunk / ch

    return (
        tile_lat_min - pad_lat,
        tile_lat_max + pad_lat,
        tile_lon_min - pad_lon,
        tile_lon_max + pad_lon,
    )


def _to_png_bytes(img_array: np.ndarray) -> bytes:
    buf = BytesIO()
    # optimize=False: RGBA bytes must be written exactly as-is for shader decoding
    Image.fromarray(img_array, "RGBA").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _interp_tile(
    ds: xr.Dataset,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    target_w: int,
    target_h: int,
) -> xr.Dataset:
    # Clamp selection to dataset bounds to minimise Zarr chunk fetches.
    # interp() handles out-of-bound target coords with NaN (→ land mask).
    sel_lat_min = max(lat_min, float(ds.lat.min()))
    sel_lat_max = min(lat_max, float(ds.lat.max()))
    sel_lon_min = max(lon_min, float(ds.lon.min()))
    sel_lon_max = min(lon_max, float(ds.lon.max()))

    ds_bbox = ds.sel(
        lat=slice(sel_lat_min, sel_lat_max),
        lon=slice(sel_lon_min, sel_lon_max),
    )

    target_lons = np.linspace(lon_min, lon_max, target_w)
    target_lats = np.linspace(lat_max, lat_min, target_h)  # north → south
    return ds_bbox.interp(lat=target_lats, lon=target_lons, method="linear")


def _encode_scalar(
    product: Product,
    ds_full: xr.Dataset,
    ds_tile: xr.Dataset,
) -> bytes:
    # val_min/val_max from the full date slice so all tiles share the same scale —
    # the manifest exposes these values and the shader uses them to decode every tile.
    val_min = float(ds_full[product.variable].min(skipna=True).values)
    val_max = float(ds_full[product.variable].max(skipna=True).values)
    if val_max == val_min:
        val_max = val_min + 1.0

    raw = ds_tile[product.variable].values.squeeze()
    ocean = (~np.isnan(raw)).astype(np.uint8)
    val_24 = np.clip(
        (np.nan_to_num(raw, nan=0.0) - val_min) / (val_max - val_min) * 16777215,
        0, 16777215,
    ).astype(np.uint32)

    h, w = val_24.shape
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[:, :, 0] = (val_24 >> 16) & 0xFF
    img[:, :, 1] = (val_24 >> 8)  & 0xFF
    img[:, :, 2] =  val_24         & 0xFF
    img[:, :, 3] = ocean * 255
    img[ocean == 0, :3] = 0  # premultiplied alpha
    return _to_png_bytes(img)


def _encode_uv(
    product: Product,
    ds_full: xr.Dataset,
    ds_tile: xr.Dataset,
) -> bytes:
    u_var, v_var = product.variable
    u_min = float(ds_full[u_var].min(skipna=True).values)
    u_max = float(ds_full[u_var].max(skipna=True).values)
    v_min = float(ds_full[v_var].min(skipna=True).values)
    v_max = float(ds_full[v_var].max(skipna=True).values)
    if u_max == u_min: u_max = u_min + 1.0
    if v_max == v_min: v_max = v_min + 1.0

    u_raw = ds_tile[u_var].values.squeeze()
    v_raw = ds_tile[v_var].values.squeeze()
    ocean = (~np.isnan(u_raw)).astype(np.uint8)
    u_norm = np.clip(
        (np.nan_to_num(u_raw, nan=0.0) - u_min) / (u_max - u_min) * 255, 0, 255
    ).astype(np.uint8)
    v_norm = np.clip(
        (np.nan_to_num(v_raw, nan=0.0) - v_min) / (v_max - v_min) * 255, 0, 255
    ).astype(np.uint8)

    h, w = u_norm.shape
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[:, :, 0] = u_norm        # R = U
    img[:, :, 1] = v_norm        # G = V
    img[:, :, 2] = ocean * 255   # B = ocean mask
    img[:, :, 3] = 255           # A = always opaque
    return _to_png_bytes(img)


def render_zarr_tile(
    product: Product,
    ds: xr.Dataset,
    lod: int,
    cx: int,
    cy: int,
) -> bytes:
    lat_min, lat_max, lon_min, lon_max = _tile_extent(product, lod, cx, cy, ds)
    cw, ch = product.chunk_px
    target_w = cw + 2 * product.padding
    target_h = ch + 2 * product.padding

    ds_tile = _interp_tile(ds, lat_min, lat_max, lon_min, lon_max, target_w, target_h)

    if isinstance(product.variable, list):
        return _encode_uv(product, ds, ds_tile)
    return _encode_scalar(product, ds, ds_tile)


def render_zarr_manifest(product: Product, ds: xr.Dataset) -> dict:
    lat_min_g = float(ds.lat.min())
    lat_max_g = float(ds.lat.max())
    lon_min_g = float(ds.lon.min())
    lon_max_g = float(ds.lon.max())

    bounds = {
        "lonMin": lon_min_g, "lonMax": lon_max_g,
        "latMin": lat_min_g, "latMax": lat_max_g,
    }
    lod_meta = {
        str(lod): {
            "grid": list(product.lod_grids[lod]),
            "chunkPx": list(product.chunk_px),
            "storedPx": [
                product.chunk_px[0] + 2 * product.padding,
                product.chunk_px[1] + 2 * product.padding,
            ],
            "padding": product.padding,
            **({"zoomThreshold": product.lod_zoom_thresholds[lod]} if lod in product.lod_zoom_thresholds else {}),
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
