from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import JSONResponse, Response

from services.colormap_store import list_colormaps
from services.visual_renderer import _colormap, render_bbox, render_tile

from .products import _get_product_or_404, _load_slice_or_404
from .products import router as products_router

router = APIRouter()
router.include_router(products_router)


def _parse_rescale(rescale: str | None) -> tuple[float, float] | None:
    if not rescale:
        return None
    try:
        lo, hi = rescale.split(",")
        return (float(lo), float(hi))
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail="rescale must be 'min,max', e.g. '-0.5,0.5'"
        ) from e


@router.get("/colormaps")
async def get_colormaps():
    return JSONResponse(content=list_colormaps())


@router.get(
    "/{product_id}/{date}/tiles/{z}/{x}/{y}.png",
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
    try:
        _colormap(colormap_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    product = _get_product_or_404(product_id)
    if isinstance(product.variable, list):
        raise HTTPException(
            status_code=400,
            detail=f"Product '{product_id}' has multiple variables; visual tiles support single-variable products only.",
        )

    max_index = (1 << z) - 1
    if not (0 <= x <= max_index and 0 <= y <= max_index):
        raise HTTPException(
            status_code=400,
            detail=f"Tile ({x},{y}) out of range for z={z}; valid range is 0–{max_index}.",
        )

    ds = _load_slice_or_404(product.source_path, date, [product.variable])

    rescale_range = _parse_rescale(rescale)

    try:
        png = render_tile(ds, product.variable, x, y, z, colormap_name, rescale_range)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=png, media_type="image/png")


@router.get(
    "/{product_id}/{date}/bbox",
    summary="Visualisation tile by bbox",
    description=(
        "Renders a colourised PNG for an arbitrary bounding box. "
        "Accepts EPSG:4326 geographic coordinates (degrees, default) or EPSG:3857 Web Mercator (meters) via the crs parameter. "
        "Compatible with Mapbox GL raster sources using the {bbox-epsg-3857} placeholder (pass crs=EPSG:3857)."
    ),
)
def get_bbox(
    product_id: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    bbox: str = Query(
        ...,
        description="Bounding box as 'minx,miny,maxx,maxy' in the CRS specified by the crs parameter.",
    ),
    width: int = Query(256, ge=1, le=2048),
    height: int = Query(256, ge=1, le=2048),
    colormap_name: str = Query("viridis", alias="colormap"),
    rescale: str | None = Query(None, description="Value range as 'min,max'."),
    crs: str = Query(
        "EPSG:4326",
        description="Coordinate reference system of the bbox. 'EPSG:4326' (default) for geographic degrees; 'EPSG:3857' for Web Mercator meters (Mapbox {bbox-epsg-3857}).",
    ),
):
    try:
        _colormap(colormap_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    product = _get_product_or_404(product_id)
    if isinstance(product.variable, list):
        raise HTTPException(
            status_code=400,
            detail=f"Product '{product_id}' has multiple variables; visual tiles support single-variable products only.",
        )

    crs = crs.upper()
    if crs not in ("EPSG:4326", "EPSG:3857"):
        raise HTTPException(status_code=400, detail="crs must be 'EPSG:4326' or 'EPSG:3857'")

    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox.split(","))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="bbox must be 'minx,miny,maxx,maxy'") from e

    rescale_range = _parse_rescale(rescale)

    ds = _load_slice_or_404(product.source_path, date, [product.variable])

    try:
        png = render_bbox(
            ds,
            product.variable,
            (minx, miny, maxx, maxy),
            width,
            height,
            colormap_name,
            rescale_range,
            crs=crs,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=png, media_type="image/png")
