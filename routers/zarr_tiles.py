import math

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from constants import ZARR_PRODUCTS
from services.zarr_loader import get_lod_grids, load_zarr_slice
from services.zarr_renderer import render_zarr_manifest, render_zarr_tile

router = APIRouter()


def _get_product_or_404(product_id: str):
    product = ZARR_PRODUCTS.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown Zarr product: {product_id}")
    return product


def _load_or_404(store_url: str, date: str, variables: list[str]):
    try:
        return load_zarr_slice(store_url, date, variables)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/{product_id}/{date}/{z}/{x}/{y}.png")
def get_tile(product_id: str, date: str, z: int, x: int, y: int):
    product = _get_product_or_404(product_id)
    lod_grids = get_lod_grids(product)

    if z not in lod_grids:
        raise HTTPException(status_code=404, detail=f"LOD {z} not available for {product_id}")

    grid_cols, grid_rows = lod_grids[z]
    if x < 0 or x >= grid_cols or y < 0 or y >= grid_rows:
        raise HTTPException(
            status_code=404, detail=f"Tile {z}/{x}/{y} out of bounds (grid {grid_cols}×{grid_rows})"
        )

    variables = product.variable if isinstance(product.variable, list) else [product.variable]
    ds = _load_or_404(product.source_path, date, variables)
    png_bytes = render_zarr_tile(product, ds, z, x, y)
    return Response(content=png_bytes, media_type="image/png")


@router.get("/{product_id}/{date}/manifest.json")
def get_manifest(product_id: str, date: str):
    product = _get_product_or_404(product_id)
    get_lod_grids(product)
    variables = product.variable if isinstance(product.variable, list) else [product.variable]
    ds = _load_or_404(product.source_path, date, variables)
    return JSONResponse(content=render_zarr_manifest(product, ds))


@router.get("/{product_id}/{date}/point")
def get_point(product_id: str, date: str, lat: float = Query(...), lon: float = Query(...)):
    product = _get_product_or_404(product_id)
    variables = product.variable if isinstance(product.variable, list) else [product.variable]
    ds = _load_or_404(product.source_path, date, variables)

    point = ds.sel(lat=lat, lon=lon, method="nearest")

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
