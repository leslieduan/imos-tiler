from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse, Response

from services.colormap_config import is_categorical, list_colormaps
from services.colormap_lookup import resolve_colormap
from services.legend_renderer import render_legend
from services.visual_renderer import render_bbox, render_tile
from utils.image import ImageFormat, media_type
from utils.memoizer import Memoizer

from .products import router as products_router
from .shared import (
    DATE_EX,
    IMMUTABLE_CACHE_HEADERS,
    PRODUCT_EX,
    get_product_or_404,
    load_slice_or_404,
)

router = APIRouter()
router.include_router(products_router)


# Dedup-only Memoizers (cache=None): /data_tiles already shares the rio-tiler /
# encoding work across concurrent requests via _processed_memo, visual tiles had
# no equivalent. With these, N concurrent identical-tile requests run one render;
# the rest block on the shared Future. No caching needed here — Cache-Control +
# browser/CDN absorb cross-request repeats.
_tile_memo: Memoizer = Memoizer()
_bbox_memo: Memoizer = Memoizer()


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


def _require_rescale_if_categorical(
    colormap_name: str, rescale_range: tuple[float, float] | None
) -> None:
    if is_categorical(colormap_name) and rescale_range is None:
        raise HTTPException(
            status_code=400,
            detail=f"Colormap '{colormap_name}' is categorical — rescale=min,max is required.",
        )


def _reject_webp_for_categorical(colormap_name: str, fmt: ImageFormat) -> None:
    # Lossy WebP introduces ringing/blocking around the hard colour boundaries
    # of a categorical colormap. PNG (or a lossless WebP, not currently exposed)
    # is the only safe choice — fail loud rather than serve a corrupted legend.
    if fmt == "webp" and is_categorical(colormap_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Colormap '{colormap_name}' is categorical and cannot be encoded as WebP "
                "(lossy compression corrupts the discrete colour boundaries). Use .png."
            ),
        )


@router.get("/colormaps", summary="List available colormaps")
async def get_colormaps():
    return JSONResponse(content=list_colormaps())


@router.get(
    "/colormaps/{name}/legend",
    summary="Color legend",
    description=(
        "Returns a PNG color legend for the named colormap. "
        "The name must be one returned by GET /visual_tiles/colormaps. "
        "If rescale=min,max is provided, tick labels at lo, mid, and hi are drawn alongside the bar. "
        "Without rescale, only the color bar is rendered (no labels). "
        "Categorical colormaps render discrete equal-width color blocks instead of a smooth gradient."
    ),
)
def get_legend(
    name: str,
    rescale: str | None = Query(
        None,
        description="Value range as 'min,max'. When provided, tick labels are drawn at lo, mid, and hi.",
    ),
    width: int = Query(256, ge=10, le=2048, description="Image width in pixels."),
    height: int = Query(40, ge=10, le=2048, description="Image height in pixels."),
    orientation: str = Query(
        "horizontal",
        description="'horizontal' (color bar left→right) or 'vertical' (color bar top→bottom).",
        pattern="^(horizontal|vertical)$",
    ),
):
    try:
        resolve_colormap(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    rescale_range = _parse_rescale(rescale)
    png = render_legend(name, rescale_range, width, height, orientation)
    return Response(content=png, media_type="image/png", headers=IMMUTABLE_CACHE_HEADERS)


@router.get(
    "/{product_id}/{date}/{z}/{x}/{y}.{ext}",
    summary="Visualisation raster tile",
    description=(
        "Standard Web Mercator (XYZ) tile rendered as a colourised PNG or WebP. "
        "Compatible with MapboxGL `raster` sources and any slippy-map library. "
        "Tiles outside the product extent return transparent images. "
        "WebP is rejected for categorical colormaps because lossy compression corrupts the discrete colour boundaries."
    ),
)
def get_tile(
    product_id: str = Path(openapi_examples=PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=DATE_EX),
    z: int = Path(openapi_examples={"default": Example(value=1)}),
    x: int = Path(openapi_examples={"default": Example(value=0)}),
    y: int = Path(openapi_examples={"default": Example(value=0)}),
    ext: ImageFormat = Path(
        pattern="^(png|webp)$",
        description="Output image format — 'png' (lossless) or 'webp' (lossy, ~50% smaller).",
    ),
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
        resolve_colormap(colormap_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    product = get_product_or_404(product_id)
    if isinstance(product.variable, list):
        raise HTTPException(
            status_code=400,
            detail=f"Product '{product_id}' has multiple variables; visual tiles support single-variable products only.",
        )
    variable: str = product.variable  # narrowed by the isinstance check above

    max_index = (1 << z) - 1
    if not (0 <= x <= max_index and 0 <= y <= max_index):
        raise HTTPException(
            status_code=400,
            detail=f"Tile ({x},{y}) out of range for z={z}; valid range is 0–{max_index}.",
        )

    rescale_range = _parse_rescale(rescale)
    _require_rescale_if_categorical(colormap_name, rescale_range)
    _reject_webp_for_categorical(colormap_name, ext)

    key = (product.source_path, date, variable, z, x, y, colormap_name, rescale_range, ext)

    def _do_render() -> bytes:
        ds = load_slice_or_404(product.source_path, date, [variable])
        return render_tile(ds, variable, x, y, z, colormap_name, rescale_range, fmt=ext)

    try:
        body = _tile_memo.get_or_compute(key, _do_render)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=body, media_type=media_type(ext), headers=IMMUTABLE_CACHE_HEADERS)


@router.get(
    "/{product_id}/{date}/bbox.{ext}",
    summary="Visualisation tile by bbox",
    description=(
        "Renders a colourised PNG or WebP for an arbitrary bounding box. "
        "Accepts EPSG:4326 geographic coordinates (degrees, default) or EPSG:3857 Web Mercator (meters) via the crs parameter. "
        "Compatible with Mapbox GL raster sources using the {bbox-epsg-3857} placeholder (pass crs=EPSG:3857). "
        "WebP is rejected for categorical colormaps because lossy compression corrupts the discrete colour boundaries."
    ),
)
def get_bbox(
    product_id: str = Path(openapi_examples=PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=DATE_EX),
    ext: ImageFormat = Path(
        pattern="^(png|webp)$",
        description="Output image format — 'png' (lossless) or 'webp' (lossy, ~50% smaller).",
    ),
    bbox: str = Query(
        "89.0,-60.0,180.0,10.0",
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
        resolve_colormap(colormap_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    product = get_product_or_404(product_id)
    if isinstance(product.variable, list):
        raise HTTPException(
            status_code=400,
            detail=f"Product '{product_id}' has multiple variables; visual tiles support single-variable products only.",
        )
    variable: str = product.variable  # narrowed by the isinstance check above

    crs = crs.upper()
    if crs not in ("EPSG:4326", "EPSG:3857"):
        raise HTTPException(status_code=400, detail="crs must be 'EPSG:4326' or 'EPSG:3857'")

    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox.split(","))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="bbox must be 'minx,miny,maxx,maxy'") from e

    rescale_range = _parse_rescale(rescale)
    _require_rescale_if_categorical(colormap_name, rescale_range)
    _reject_webp_for_categorical(colormap_name, ext)

    key = (
        product.source_path,
        date,
        variable,
        (minx, miny, maxx, maxy),
        width,
        height,
        crs,
        colormap_name,
        rescale_range,
        ext,
    )

    def _do_render() -> bytes:
        ds = load_slice_or_404(product.source_path, date, [variable])
        return render_bbox(
            ds,
            variable,
            (minx, miny, maxx, maxy),
            width,
            height,
            colormap_name,
            rescale_range,
            crs=crs,
            fmt=ext,
        )

    try:
        body = _bbox_memo.get_or_compute(key, _do_render)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=body, media_type=media_type(ext), headers=IMMUTABLE_CACHE_HEADERS)
