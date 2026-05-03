import math

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from constants import PRODUCTS
from services.netcdf_loader import load_dataset
from services.netcdf_renderer import render_manifest, render_tile

router = APIRouter()


def _get_product_or_404(product_id: str):
    product = PRODUCTS.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product_id}")
    return product


def _load_or_404(source_path: str, date: str):
    try:
        return load_dataset(source_path, date)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/{product_id}/{date}/{z}/{x}/{y}.png")
def get_tile(product_id: str, date: str, z: int, x: int, y: int):
    product = _get_product_or_404(product_id)

    if z not in product.lod_grids:
        raise HTTPException(status_code=404, detail=f"LOD {z} not available for {product_id}")

    grid_cols, grid_rows = product.lod_grids[z]
    if x < 0 or x >= grid_cols or y < 0 or y >= grid_rows:
        raise HTTPException(
            status_code=404, detail=f"Tile {z}/{x}/{y} out of bounds (grid {grid_cols}×{grid_rows})"
        )

    ds = _load_or_404(product.source_path, date)
    png_bytes = render_tile(product, ds, z, x, y)
    return Response(content=png_bytes, media_type="image/png")


@router.get("/{product_id}/{date}/manifest.json")
def get_manifest(product_id: str, date: str):
    product = _get_product_or_404(product_id)
    ds = _load_or_404(product.source_path, date)
    return JSONResponse(content=render_manifest(product, ds))


@router.get("/{product_id}/{date}/point")
def get_point(product_id: str, date: str, lat: float = Query(...), lon: float = Query(...)):
    # This is expected to be called after get_manifest and get_tile. Since variable value will be cached by either one, the response in this will be quick.
    product = _get_product_or_404(product_id)
    ds = _load_or_404(product.source_path, date)

    # Lazy load only read this point.
    point = ds.sel(lat=lat, lon=lon, method="nearest")

    variables = product.variable if isinstance(product.variable, list) else [product.variable]
    values = {}
    for var in variables:
        v = float(point[var].squeeze())
        values[var] = {
            "value": None if math.isnan(v) else v,
            "units": point[var].attrs.get("units"),
        }

    return JSONResponse(
        content={
            "lat": float(point.lat.values),
            "lon": float(point.lon.values),
            "variables": values,
        }
    )
