from fastapi import APIRouter, HTTPException, Path, Response
from fastapi.openapi.models import Example

from app.schemas.data_tiles import DataTileManifestResponse
from app.services.product.manifest import render_manifest
from app.services.product.product import get_lod_grids
from app.services.rendering.data_tiles import render_tile

from ..shared import (
    DATE_EX,
    IMMUTABLE_CACHE_HEADERS,
    PRODUCT_EX,
    get_product_or_404,
    load_slice_or_404,
    validate_date,
)
from .products import router as products_router

router = APIRouter()
router.include_router(products_router)


@router.get(
    "/{product_id}/{date}/{z}/{x}/{y}.png",
    summary="Raw data tile",
    description=(
        "Returns an RGBA PNG encoded for WebGL shader consumption. "
        "Scalar products use R/G/B as a 24-bit normalised uint; UV vector products pack U in R and V in G. "
        "Fetch the manifest first to get the normalisation ranges needed for decoding."
    ),
)
def get_tile(
    product_id: str = Path(openapi_examples=PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=DATE_EX),
    z: int = Path(openapi_examples={"default": Example(value=1)}),
    x: int = Path(openapi_examples={"default": Example(value=0)}),
    y: int = Path(openapi_examples={"default": Example(value=0)}),
):
    product = get_product_or_404(product_id)
    validate_date(date)
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
        lambda: load_slice_or_404(product.source_path, date, variables),
        z,
        x,
        y,
        date,
    )
    return Response(content=png_bytes, media_type="image/png", headers=IMMUTABLE_CACHE_HEADERS)


@router.get(
    "/{product_id}/{date}/manifest.json",
    summary="Data tile manifest",
    description=(
        "Returns the LOD grid dimensions and value normalisation ranges for a product on a given date. "
        "Required for decoding raw data tiles — provides `valueRange` for scalar products and `uRange`/`vRange` for UV vector products."
    ),
    response_model=DataTileManifestResponse,
    response_model_exclude_none=True,
)
def get_manifest(
    response: Response,
    product_id: str = Path(openapi_examples=PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=DATE_EX),
):
    product = get_product_or_404(product_id)
    validate_date(date)
    get_lod_grids(product)
    variables = product.variables
    ds = load_slice_or_404(product.source_path, date, variables)
    response.headers.update(IMMUTABLE_CACHE_HEADERS)
    return DataTileManifestResponse(**render_manifest(product, ds))
