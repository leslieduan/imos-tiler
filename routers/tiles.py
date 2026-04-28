import threading

import xarray as xr
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from constants import Product, PRODUCTS
from services.loader import load_dataset
from services.renderer import _get_resampled, render_manifest, render_tile

router = APIRouter()


def _prewarm(product: Product, ds: xr.Dataset) -> None:
    for lod in product.lod_grids:
        _get_resampled(ds, product, lod)


def _get_product_or_404(product_id: str):
    product = PRODUCTS.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product_id}")
    return product


def _load_or_404(product_id: str, date: str):
    try:
        return load_dataset(product_id, date)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{product_id}/{date}/{z}/{x}/{y}.png")
def get_tile(product_id: str, date: str, z: int, x: int, y: int):
    product = _get_product_or_404(product_id)

    if z not in product.lod_grids:
        raise HTTPException(status_code=404, detail=f"LOD {z} not available for {product_id}")

    grid_cols, grid_rows = product.lod_grids[z]
    if x < 0 or x >= grid_cols or y < 0 or y >= grid_rows:
        raise HTTPException(status_code=404, detail=f"Tile {z}/{x}/{y} out of bounds (grid {grid_cols}×{grid_rows})")

    ds = _load_or_404(product_id, date)
    png_bytes = render_tile(product, ds, z, x, y)
    return Response(content=png_bytes, media_type="image/png")


@router.get("/{product_id}/{date}/manifest.json")
def get_manifest(product_id: str, date: str):
    product = _get_product_or_404(product_id)
    ds = _load_or_404(product_id, date)
    threading.Thread(target=_prewarm, args=(product, ds), daemon=True).start()
    return JSONResponse(content=render_manifest(product, ds))
