from fastapi import APIRouter, HTTPException, Path
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse, Response

from services.data_renderer import render_manifest, render_tile
from services.loader import get_lod_grids

from .products import _DATE_EX, _PRODUCT_EX, _get_product_or_404, _load_slice_or_404
from .products import router as products_router

router = APIRouter()
router.include_router(products_router)


@router.get(
    "/{product_id}/{date}/tiles/{z}/{x}/{y}.png",
    summary="Raw data tile",
    description=(
        "Returns an RGBA PNG encoded for WebGL shader consumption. "
        "Scalar products use R/G/B as a 24-bit normalised uint; UV vector products pack U in R and V in G. "
        "Fetch the manifest first to get the normalisation ranges needed for decoding."
    ),
)
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


@router.get(
    "/{product_id}/{date}/manifest.json",
    summary="Data tile manifest",
    description=(
        "Returns the LOD grid dimensions and value normalisation ranges for a product on a given date. "
        "Required for decoding raw data tiles — provides `valueRange` for scalar products and `uRange`/`vRange` for UV vector products."
    ),
)
def get_manifest(
    product_id: str = Path(openapi_examples=_PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=_DATE_EX),
):
    product = _get_product_or_404(product_id)
    get_lod_grids(product)
    variables = product.variables
    ds = _load_slice_or_404(product.source_path, date, variables)
    return JSONResponse(content=render_manifest(product, ds))
