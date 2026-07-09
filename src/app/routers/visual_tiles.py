import asyncio
import functools

import anyio
from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.openapi.models import Example
from fastapi.responses import Response

from app.config.settings import ANIMATION_WORKERS
from app.schemas.visual_tiles import ColormapListResponse
from app.services.caching.deduper import Deduper
from app.services.colormap.legend import render_legend
from app.services.colormap.registry import list_colormaps
from app.services.rendering.visual_tiles import (
    render_bbox,
    render_bbox_animation,
    render_tile,
)
from app.services.store.registry import get_available_dates
from app.services.store.slice_loader import load_slice_uncached
from app.services.store.spatial import (
    bbox_to_wgs84,
    default_bbox_from_store,
    native_resolution_in_bbox,
)
from app.utils.image import AnimatedFormat, ImageFormat, animated_media_type, media_type

from .products import router as products_router
from .shared import (
    DATE_EX,
    IMMUTABLE_CACHE_HEADERS,
    PRODUCT_EX,
    get_product_or_404,
    load_slice_or_404,
    parse_rescale,
    resolve_colormap_or_error,
    single_variable_or_400,
    validate_date,
)

_MAX_ANIMATION_FRAMES = 30

# Capacity gate for /animation per-frame S3 fan-out. Sits on the shared anyio
# pool as a *separate* concurrency budget from tile handlers — a 30-frame
# request cannot starve tile-handler slots. Sized to the aiobotocore S3
# connection-pool ceiling (~10/host) — going higher just queues on the pool.
_ANIMATION_LIMITER = anyio.CapacityLimiter(ANIMATION_WORKERS)

router = APIRouter()
router.include_router(products_router)

_tile_dedup = Deduper()
_bbox_dedup = Deduper()


@router.get("/colormaps", summary="List available colormaps", response_model=ColormapListResponse)
async def get_colormaps():
    return ColormapListResponse(**list_colormaps())


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
        description=(
            "Value range as 'min,max'. When provided, tick labels are drawn at lo, mid, and hi. "
            "Rejected for categorical colormaps, whose discrete blocks have no scale to label."
        ),
    ),
    width: int = Query(256, ge=10, le=2048, description="Image width in pixels."),
    height: int = Query(40, ge=10, le=2048, description="Image height in pixels."),
    orientation: str = Query(
        "horizontal",
        description="'horizontal' (color bar left→right) or 'vertical' (color bar top→bottom).",
        pattern="^(horizontal|vertical)$",
    ),
):
    resolve_colormap_or_error(name, status_code=404)
    rescale_range = parse_rescale(rescale)
    try:
        png = render_legend(name, rescale_range, width, height, orientation)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
    colormap_name: str | None = Query(
        None,
        alias="colormap",
        description=(
            "Matplotlib or rio-tiler colormap name, e.g. viridis, plasma, RdBu_r. "
            "Omit to use the default (viridis for continuous products, the categorical "
            "palette for flag-valued products). Passing a continuous colormap to a "
            "categorical product is rejected."
        ),
    ),
    rescale: str | None = Query(
        None,
        description=(
            "Value range as 'min,max'. Defaults to the global data range for the date. "
            "Rejected for categorical products, which have no continuous scale to rescale."
        ),
    ),
):
    if colormap_name is not None:
        resolve_colormap_or_error(colormap_name)
    product = get_product_or_404(product_id)
    validate_date(date)
    variable = single_variable_or_400(product, context="visual tiles")

    max_index = (1 << z) - 1
    if not (0 <= x <= max_index and 0 <= y <= max_index):
        raise HTTPException(
            status_code=400,
            detail=f"Tile ({x},{y}) out of range for z={z}; valid range is 0–{max_index}.",
        )

    rescale_range = parse_rescale(rescale)

    key = (product.source_path, date, variable, z, x, y, colormap_name, rescale_range, ext)

    def _do_render() -> bytes:
        ds = load_slice_or_404(
            product.source_path, date, [variable], ocean_masked=product.ocean_masked
        )
        return render_tile(ds, variable, x, y, z, colormap_name, rescale_range, fmt=ext)

    try:
        body = _tile_dedup.dedupe(key, _do_render)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=body, media_type=media_type(ext), headers=IMMUTABLE_CACHE_HEADERS)


def _resolve_resolution(
    product_source_path: str,
    bbox_tuple: tuple[float, float, float, float],
    crs: str,
    width: int | None,
    height: int | None,
    max_dim: int = 2048,
) -> tuple[int, int]:
    """Fill in missing width/height per the documented defaulting rules.

    Both omitted → dataset native cell count inside the bbox.
    One provided → the other is derived from the bbox aspect ratio (in the bbox's
    own CRS), so the output frame is not stretched relative to the requested view.
    """
    if width is not None and height is not None:
        return width, height

    if width is None and height is None:
        bbox_wgs84 = bbox_to_wgs84(bbox_tuple, crs)
        return native_resolution_in_bbox(product_source_path, bbox_wgs84, max_dim)

    minx, miny, maxx, maxy = bbox_tuple
    span_x = (maxx - minx) or 1.0
    span_y = (maxy - miny) or 1.0
    aspect = span_x / span_y

    if height is None:
        # Exactly one is None at this point — narrow with a runtime check for mypy.
        assert width is not None
        derived_h = max(1, min(max_dim, int(round(width / aspect))))
        return width, derived_h

    assert width is None
    derived_w = max(1, min(max_dim, int(round(height * aspect))))
    return derived_w, height


def _parse_bbox_and_crs(
    bbox: str | None, crs: str, source_path: str
) -> tuple[tuple[float, float, float, float], str]:
    """Validate the crs param and parse the bbox string.

    When bbox is None, falls back to the dataset's native bounds and forces
    crs back to EPSG:4326 (the bounds are always reported in WGS84).
    """
    crs = crs.upper()
    if crs not in ("EPSG:4326", "EPSG:3857"):
        raise HTTPException(status_code=400, detail="crs must be 'EPSG:4326' or 'EPSG:3857'")
    if bbox is None:
        return default_bbox_from_store(source_path), "EPSG:4326"
    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox.split(","))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="bbox must be 'minx,miny,maxx,maxy'") from e
    return (minx, miny, maxx, maxy), crs


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
    colormap_name: str | None = Query(
        None,
        alias="colormap",
        description=(
            "Colormap name. Omit to use the default (viridis for continuous products, the "
            "categorical palette for flag-valued products). A continuous colormap on a "
            "categorical product is rejected."
        ),
    ),
    rescale: str | None = Query(
        None,
        description=(
            "Value range as 'min,max'. Rejected for categorical products, which have no "
            "continuous scale to rescale."
        ),
    ),
    crs: str = Query(
        "EPSG:4326",
        description="Coordinate reference system of the bbox. 'EPSG:4326' (default) for geographic degrees; 'EPSG:3857' for Web Mercator meters (Mapbox {bbox-epsg-3857}).",
    ),
):
    if colormap_name is not None:
        resolve_colormap_or_error(colormap_name)
    product = get_product_or_404(product_id)
    validate_date(date)
    variable = single_variable_or_400(product, context="visual tiles")

    bbox_tuple, crs = _parse_bbox_and_crs(bbox, crs, product.source_path)

    rescale_range = parse_rescale(rescale)

    key = (
        product.source_path,
        date,
        variable,
        bbox_tuple,
        width,
        height,
        crs,
        colormap_name,
        rescale_range,
        ext,
    )

    def _do_render() -> bytes:
        ds = load_slice_or_404(
            product.source_path, date, [variable], ocean_masked=product.ocean_masked
        )
        return render_bbox(
            ds,
            variable,
            bbox_tuple,
            width,
            height,
            colormap_name,
            rescale_range,
            crs=crs,
            fmt=ext,
        )

    try:
        body = _bbox_dedup.dedupe(key, _do_render)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=body, media_type=media_type(ext), headers=IMMUTABLE_CACHE_HEADERS)


@router.get(
    "/{product_id}/{from_date}/{to_date}/animation.{ext}",
    summary="Animated bbox over a date range",
    description=(
        f"Renders the same bbox across every available date in [from_date, to_date] "
        f"and assembles them into an animated image (GIF / APNG / animated WebP). "
        f"Intended for demos and quick visualisations — not optimised for high traffic. "
        f"At most {_MAX_ANIMATION_FRAMES} frames per request; requests beyond that are rejected. "
        f"If bbox is omitted, the dataset's native bounds are used (clamped to ±180° lon). "
        f"If width and height are both omitted, the frame matches the dataset's native cell count "
        f"inside the bbox (capped at 2048 px per axis). If only one of width/height is given, "
        f"the other is derived from the bbox aspect ratio so the output is not stretched. "
        f"This endpoint bypasses the in-memory slice cache so it never evicts hot tiles, "
        f"and the response is not cached. Expect cold requests to be slow."
    ),
)
async def get_animation(
    product_id: str = Path(openapi_examples=PRODUCT_EX),
    from_date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=DATE_EX),
    to_date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=DATE_EX),
    ext: AnimatedFormat = Path(
        pattern="^(gif|apng|webp)$",
        description="Animated output format — 'gif' (universal, 256-colour palette), 'apng' (lossless RGBA), or 'webp' (compressed RGBA).",
    ),
    bbox: str | None = Query(
        None,
        description="Bounding box as 'minx,miny,maxx,maxy' in the CRS specified by the crs parameter. Defaults to the dataset's native bounds.",
    ),
    width: int | None = Query(
        None,
        ge=1,
        le=2048,
        description=(
            "Output frame width in pixels. If both width and height are omitted, the frame matches the "
            "dataset's native cell count inside the bbox (capped at 2048). If only height is given, "
            "width is derived from the bbox aspect ratio."
        ),
    ),
    height: int | None = Query(
        None,
        ge=1,
        le=2048,
        description=(
            "Output frame height in pixels. If both width and height are omitted, the frame matches the "
            "dataset's native cell count inside the bbox (capped at 2048). If only width is given, "
            "height is derived from the bbox aspect ratio."
        ),
    ),
    colormap_name: str | None = Query(
        None,
        alias="colormap",
        description=(
            "Colormap name. Omit to use the default (viridis for continuous products, the "
            "categorical palette for flag-valued products). A continuous colormap on a "
            "categorical product is rejected."
        ),
    ),
    rescale: str | None = Query(
        None,
        description=(
            "Value range as 'min,max'. Defaults to the union range across all frames so the "
            "colour ramp stays stable. Rejected for categorical products, which have no "
            "continuous scale to rescale."
        ),
    ),
    crs: str = Query(
        "EPSG:4326",
        description="CRS of the bbox. 'EPSG:4326' (default) for geographic degrees; 'EPSG:3857' for Web Mercator meters.",
    ),
    duration: int = Query(
        200, ge=10, le=5000, description="Milliseconds per frame in the animation."
    ),
):
    validate_date(from_date)
    validate_date(to_date)
    if from_date > to_date:
        raise HTTPException(
            status_code=400, detail=f"from_date {from_date!r} is after to_date {to_date!r}."
        )

    if colormap_name is not None:
        resolve_colormap_or_error(colormap_name)
    product = get_product_or_404(product_id)
    variable = single_variable_or_400(product, context="animation")

    # Offloaded: each may call get_store, which can block on xr.open_zarr on
    # cold path or while a TTL refresh is racing the cached entry.
    bbox_tuple, crs = await anyio.to_thread.run_sync(
        _parse_bbox_and_crs, bbox, crs, product.source_path
    )

    rescale_range = parse_rescale(rescale)
    # Categorical validation (format, colormap↔variable fit) runs inside
    # render_bbox_animation, where the loaded slice's attrs are available; a
    # ValueError there is mapped to 400 below.

    available = await anyio.to_thread.run_sync(get_available_dates, product.source_path)
    if not available:
        raise HTTPException(
            status_code=404,
            detail=f"No data available for product {product_id!r}.",
        )
    earliest, latest = available[0], available[-1]
    if from_date < earliest or to_date > latest:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Requested range [{from_date}, {to_date}] is outside the available dates "
                f"for product {product_id!r} ([{earliest}, {latest}])."
            ),
        )
    dates = [d for d in available if from_date <= d <= to_date]
    if not dates:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No data for product {product_id!r} between {from_date} and {to_date} "
                f"(available range: [{earliest}, {latest}])."
            ),
        )
    if len(dates) > _MAX_ANIMATION_FRAMES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Date range yields {len(dates)} frames; max is {_MAX_ANIMATION_FRAMES}. "
                "Narrow the range and retry."
            ),
        )

    resolved_w, resolved_h = await anyio.to_thread.run_sync(
        _resolve_resolution, product.source_path, bbox_tuple, crs, width, height
    )

    # Fan out the per-frame S3 reads in parallel on the anyio pool, gated by
    # _ANIMATION_LIMITER so a many-frame request does not consume tile-handler
    # slots. asyncio.gather preserves input order so frames stay in date order.
    datasets = await asyncio.gather(
        *(
            anyio.to_thread.run_sync(
                load_slice_uncached,
                product.source_path,
                d,
                [variable],
                product.ocean_masked,
                limiter=_ANIMATION_LIMITER,
            )
            for d in dates
        )
    )

    try:
        body = await anyio.to_thread.run_sync(
            functools.partial(
                render_bbox_animation,
                datasets,
                variable,
                bbox_tuple,
                resolved_w,
                resolved_h,
                colormap_name,
                rescale_range,
                crs=crs,
                fmt=ext,
                duration_ms=duration,
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=body, media_type=animated_media_type(ext))
