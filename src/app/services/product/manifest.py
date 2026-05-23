"""Build the JSON manifest returned by ``/data_tiles/{product}/{date}/manifest.json``.

Pure product introspection: takes a product and a slice dataset, returns the
bounds + per-variable value range + per-LOD grid metadata the WebGL shader
needs to decode raw data tiles. No rendering, no caching — lives next to the
product domain rather than the rendering pipeline because the output is a
description of the product's data shape on this date, not a pixel artifact.
"""

from typing import Any

import xarray as xr

from app.constants import LOD
from app.services.product.product import Product
from app.utils.geo import json_safe_float


def render_manifest(product: Product, ds: xr.Dataset) -> dict[str, Any]:
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
