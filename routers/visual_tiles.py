from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import Response

from services.loader import load_slice
from services.visual_renderer import render_tile

from .products import _get_product_or_404
from .products import router as products_router

router = APIRouter()
router.include_router(products_router)


@router.get(
    "/{product_id}/{date}/{z}/{x}/{y}.png",
    summary="Visualisation raster tile",
    description=(
        "Standard Web Mercator (XYZ) tile rendered as a colourised PNG. "
        "Compatible with MapboxGL `raster` sources and any slippy-map library. "
        "Tiles outside the product extent return transparent PNGs."
    ),
)
def get_tile(
    product_id: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    z: int = Path(...),
    x: int = Path(...),
    y: int = Path(...),
    colormap_name: str = Query(
        "viridis",
        alias="colormap",
        description="Matplotlib or rio-tiler colormap name, e.g. viridis, plasma, RdBu_r.",
    ),
    rescale: str | None = Query(
        None,
        description="Value range as 'min,max'. Defaults to the global data range for the date.",
    ),
):
    product = _get_product_or_404(product_id)
    if isinstance(product.variable, list):
        raise HTTPException(
            status_code=400,
            detail=f"Product '{product_id}' has multiple variables; visual tiles support single-variable products only.",
        )

    try:
        ds = load_slice(product.source_path, date, [product.variable])
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    rescale_range: tuple[float, float] | None = None
    if rescale:
        try:
            lo, hi = rescale.split(",")
            rescale_range = (float(lo), float(hi))
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail="rescale must be 'min,max', e.g. '-0.5,0.5'"
            ) from e

    try:
        png = render_tile(ds, product.variable, x, y, z, colormap_name, rescale_range)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=png, media_type="image/png")
