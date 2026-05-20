import asyncio

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.openapi.models import Example
from fastapi.responses import Response

from app.schemas.visual_tiles import ColormapListResponse
from app.services.colormap_config import is_categorical, list_colormaps
from app.services.colormap_lookup import resolve_colormap
from app.services.legend_renderer import render_legend
from app.services.loader import get_available_dates, load_slice_uncached
from app.services.store_registry import get_store
from app.services.visual_renderer import (
    _bbox_to_wgs84,
    render_bbox,
    render_bbox_animation,
    render_tile,
)
from app.utils.image import AnimatedFormat, ImageFormat, animated_media_type, media_type
from app.utils.memoizer import Memoizer

from .products import router as products_router
from .shared import (
    DATE_EX,
    IMMUTABLE_CACHE_HEADERS,
    PRODUCT_EX,
    get_product_or_404,
    load_slice_or_404,
    validate_date,
)

_MAX_ANIMATION_FRAMES = 60

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
    validate_date(date)
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


def _native_resolution_in_bbox(
    product_source_path: str,
    bbox_wgs84: tuple[float, float, float, float],
    max_dim: int = 2048,
) -> tuple[int, int]:
    """Output dimensions that match the dataset's native cell resolution inside the bbox.

    Clamped to ``[1, max_dim]`` per axis so a huge bbox over a high-resolution grid
    can't blow the response up to an unreasonable size. Cell spacing is read from
    the first two lat/lon coordinates — all current products are on regular grids;
    irregular grids would need a different code path.
    """
    store = get_store(product_source_path)
    lat_vals = store.lat.values
    lon_vals = store.lon.values
    lat_spacing = abs(float(lat_vals[1] - lat_vals[0]))
    lon_spacing = abs(float(lon_vals[1] - lon_vals[0]))
    lon_min, lat_min, lon_max, lat_max = bbox_wgs84
    w = max(1, min(max_dim, int(round((lon_max - lon_min) / lon_spacing))))
    h = max(1, min(max_dim, int(round((lat_max - lat_min) / lat_spacing))))
    return w, h


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
        bbox_wgs84 = _bbox_to_wgs84(bbox_tuple, crs)
        return _native_resolution_in_bbox(product_source_path, bbox_wgs84, max_dim)

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


def _default_bbox_from_store(product_source_path: str) -> tuple[float, float, float, float]:
    """Return EPSG:4326 bounds for the dataset, clamped to ±180 lon.

    Antimeridian-straddling datasets (e.g. GSLA at 57–185°E) lose the sliver past
    180° in the default rendering — callers can pass an explicit bbox to cover
    the other side.
    """
    store = get_store(product_source_path)
    lat_min = float(store.lat.min())
    lat_max = float(store.lat.max())
    lon_min = float(store.lon.min())
    lon_max = float(store.lon.max())
    if lon_min > 180:
        lon_min -= 360
    if lon_max > 180:
        lon_max = 180.0
    return (lon_min, lat_min, lon_max, lat_max)


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
    validate_date(date)
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
# async because we want to parallelise the per-frame S3 reads, which are the bottleneck for a multi-frame animation.
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
    colormap_name: str = Query("viridis", alias="colormap"),
    rescale: str | None = Query(
        None,
        description="Value range as 'min,max'. Defaults to the union range across all frames so the colour ramp stays stable.",
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

    try:
        resolve_colormap(colormap_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    product = get_product_or_404(product_id)
    if isinstance(product.variable, list):
        raise HTTPException(
            status_code=400,
            detail=f"Product '{product_id}' has multiple variables; animation supports single-variable products only.",
        )
    variable: str = product.variable

    crs = crs.upper()
    if crs not in ("EPSG:4326", "EPSG:3857"):
        raise HTTPException(status_code=400, detail="crs must be 'EPSG:4326' or 'EPSG:3857'")

    if bbox is None:
        bbox_tuple = _default_bbox_from_store(product.source_path)
        # Default bbox is in EPSG:4326 regardless of the crs query param.
        crs = "EPSG:4326"
    else:
        try:
            minx, miny, maxx, maxy = (float(v) for v in bbox.split(","))
        except ValueError as e:
            raise HTTPException(status_code=400, detail="bbox must be 'minx,miny,maxx,maxy'") from e
        bbox_tuple = (minx, miny, maxx, maxy)

    rescale_range = _parse_rescale(rescale)
    _require_rescale_if_categorical(colormap_name, rescale_range)
    if ext == "webp" and is_categorical(colormap_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Colormap '{colormap_name}' is categorical and cannot be encoded as animated WebP "
                "(lossy compression corrupts the discrete colour boundaries). Use .apng or .gif."
            ),
        )

    available = get_available_dates(product.source_path)
    dates = [d for d in available if from_date <= d <= to_date]
    if not dates:
        raise HTTPException(
            status_code=404,
            detail=f"No data for product {product_id!r} in [{from_date}, {to_date}].",
        )
    if len(dates) > _MAX_ANIMATION_FRAMES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Date range yields {len(dates)} frames; max is {_MAX_ANIMATION_FRAMES}. "
                "Narrow the range and retry."
            ),
        )

    resolved_w, resolved_h = _resolve_resolution(
        product.source_path, bbox_tuple, crs, width, height
    )

    # Fan out the per-frame S3 reads in parallel: each load_slice_uncached call blocks
    # on Zarr/S3, so asyncio.to_thread frees the event loop and asyncio.gather drops
    # total latency to ~max(per-frame) instead of the serial sum. Frames stay in the
    # original date order because gather preserves the input order.
    datasets = await asyncio.gather(
        *(asyncio.to_thread(load_slice_uncached, product.source_path, d, [variable]) for d in dates)
    )

    try:
        body = render_bbox_animation(
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Response(content=body, media_type=animated_media_type(ext))
