from fastapi import APIRouter, HTTPException, Path
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse, Response

from services.data_renderer import render_manifest, render_tile
from services.loader import get_lod_grids

from .products import _get_product_or_404, _load_slice_or_404
from .products import router as products_router

router = APIRouter()
router.include_router(products_router)

_PRODUCT_EX: dict[str, Example] = {"default": Example(value="sea_level_anomaly")}
_DATE_EX: dict[str, Example] = {"default": Example(value="2024-02-24")}


@router.get("/{product_id}/{date}/tiles/{z}/{x}/{y}.png")
def get_tile(
    product_id: str = Path(openapi_examples=_PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=_DATE_EX),
    z: int = Path(openapi_examples={"default": Example(value=1)}),
    x: int = Path(openapi_examples={"default": Example(value=0)}),
    y: int = Path(openapi_examples={"default": Example(value=0)}),
):
    product = _get_product_or_404(product_id)
    lod_grids = get_lod_grids(product)

    if z not in lod_grids:
        raise HTTPException(status_code=404, detail=f"LOD {z} not available for {product_id}")

    grid_cols, grid_rows = lod_grids[z]
    if x < 0 or x >= grid_cols or y < 0 or y >= grid_rows:
        raise HTTPException(
            status_code=404, detail=f"Tile {z}/{x}/{y} out of bounds (grid {grid_cols}×{grid_rows})"
        )

    variables = product.variables
    png_bytes = render_tile(
        product,
        lambda: _load_slice_or_404(product.source_path, date, variables),
        z,
        x,
        y,
        date,
    )
    return Response(content=png_bytes, media_type="image/png")


@router.get("/{product_id}/{date}/manifest.json")
def get_manifest(
    product_id: str = Path(openapi_examples=_PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=_DATE_EX),
):
    product = _get_product_or_404(product_id)
    get_lod_grids(product)
    variables = product.variables
    ds = _load_slice_or_404(product.source_path, date, variables)
    return JSONResponse(content=render_manifest(product, ds))
