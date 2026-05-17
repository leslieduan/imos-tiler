import concurrent.futures
import threading
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse, Response

from services.colormap_store import is_categorical, list_colormaps
from services.visual_renderer import _colormap, render_bbox, render_legend, render_tile

from .products import _DATE_EX, _PRODUCT_EX, _get_product_or_404, _load_slice_or_404
from .products import router as products_router

router = APIRouter()
router.include_router(products_router)


_IMAGE_CACHE_HEADERS = {"Cache-Control": f"public, max-age={86400 * 30}"}

# Concurrent-render dedup. /data_tiles already shares the rio-tiler/encoding work
# across concurrent requests via services.data_renderer._processed_inflight; visual
# tiles had no equivalent. On a cold cache, a busy viewport with N clients hitting
# the same (product, date, z, x, y, colormap, rescale) tile previously ran N
# independent XarrayReader reprojects + encodes. With this dedup the first request
# does the work and the rest wait on the same Future.
#
# Sized by tile *signature*, not bytes: dict only holds at most one entry per
# in-flight key, and entries are removed in `finally`. No unbounded growth.
_tile_inflight: dict[tuple, "concurrent.futures.Future[bytes]"] = {}
_tile_lock = threading.Lock()
_bbox_inflight: dict[tuple, "concurrent.futures.Future[bytes]"] = {}
_bbox_lock = threading.Lock()


def _deduped(
    key: tuple,
    lock: threading.Lock,
    inflight: dict[tuple, "concurrent.futures.Future[bytes]"],
    fn: Callable[[], bytes],
) -> bytes:
    """Run fn() once per concurrent key; concurrent callers receive the same result.

    Errors propagate to all waiters so a failed request doesn't permanently block
    future attempts for the same key (the entry is popped in `finally`).
    """
    should_compute = False
    with lock:
        if key in inflight:
            future = inflight[key]
        else:
            future = concurrent.futures.Future()
            inflight[key] = future
            should_compute = True

    if not should_compute:
        return future.result()

    try:
        result = fn()
        future.set_result(result)
    except Exception as e:
        future.set_exception(e)
        raise
    finally:
        with lock:
            inflight.pop(key, None)
    return result


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
        _colormap(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    rescale_range = _parse_rescale(rescale)
    png = render_legend(name, rescale_range, width, height, orientation)
    return Response(content=png, media_type="image/png", headers=_IMAGE_CACHE_HEADERS)


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
    product_id: str = Path(openapi_examples=_PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=_DATE_EX),
    z: int = Path(openapi_examples={"default": Example(value=1)}),
    x: int = Path(openapi_examples={"default": Example(value=0)}),
    y: int = Path(openapi_examples={"default": Example(value=0)}),
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

    rescale_range = _parse_rescale(rescale)
    _require_rescale_if_categorical(colormap_name, rescale_range)

    key = (product.source_path, date, product.variable, z, x, y, colormap_name, rescale_range)

    def _do_render() -> bytes:
        ds = _load_slice_or_404(product.source_path, date, [product.variable])
        return render_tile(ds, product.variable, x, y, z, colormap_name, rescale_range)

    try:
        png = _deduped(key, _tile_lock, _tile_inflight, _do_render)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=png, media_type="image/png", headers=_IMAGE_CACHE_HEADERS)


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
    product_id: str = Path(openapi_examples=_PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=_DATE_EX),
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
    _require_rescale_if_categorical(colormap_name, rescale_range)

    key = (
        product.source_path,
        date,
        product.variable,
        (minx, miny, maxx, maxy),
        width,
        height,
        crs,
        colormap_name,
        rescale_range,
    )

    def _do_render() -> bytes:
        ds = _load_slice_or_404(product.source_path, date, [product.variable])
        return render_bbox(
            ds,
            product.variable,
            (minx, miny, maxx, maxy),
            width,
            height,
            colormap_name,
            rescale_range,
            crs=crs,
        )

    try:
        png = _deduped(key, _bbox_lock, _bbox_inflight, _do_render)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=png, media_type="image/png", headers=_IMAGE_CACHE_HEADERS)
