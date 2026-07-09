"""Web Mercator tile and bbox rendering for the visual_tiles router.

Resamples a 2-D scalar field through rio-tiler's XarrayReader, applies a
colormap LUT from [[colormap.resolver]], and encodes as PNG or WebP. The
antimeridian split in `_to_scalar_parts` is the one non-obvious bit — regional
grids that cross 180° E (e.g. GSLA 57–185°E) are split into two segments so
each fits inside rio_tiler's strict ±180 bound; the parts are composited as
numpy arrays before the single image encode.
"""

import traceback
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import xarray as xr
from rio_tiler.colormap import apply_cmap
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io.xarray import XarrayReader
from rio_tiler.models import ImageData
from rioxarray.exceptions import NoDataInBounds

from app.services.colormap.categorical import RGBA, is_categorical_variable, resolve_scheme
from app.services.colormap.registry import get_category_values, is_categorical
from app.services.colormap.resolver import resolve_colormap
from app.services.store.spatial import bbox_to_wgs84
from app.utils.image import (
    AnimatedFormat,
    ImageFormat,
    empty_tile,
    encode_rgba,
    encode_rgba_animation,
)


def warmup_visual() -> None:
    """Prime rio_tiler + GDAL warp so the first visual tile request doesn't pay
    one-time init overhead (warp kernel, projection database, rio_tiler internals).
    Synchronous; intended to be called once during startup.
    """
    da = xr.DataArray(
        np.zeros((16, 16), dtype=np.float32),
        dims=("lat", "lon"),
        coords={"lat": np.linspace(1.0, 0.0, 16), "lon": np.linspace(0.0, 1.0, 16)},
    )
    da = _apply_crs(da)
    # Synthetic grayscale LUT — avoids depending on the colormap registry being loaded.
    cm = {i: (i, i, i, 255) for i in range(256)}
    try:
        with XarrayReader(da) as reader:
            img = reader.tile(0, 0, 0, reproject_method="bilinear")
        img.rescale(in_range=[(0.0, 1.0)])
        _img_to_rgba(img, cm)
    except Exception:
        print("Visual warmup failed")
        traceback.print_exc()


def _img_to_rgba(img: ImageData, cm: dict[int, tuple[int, int, int, int]]) -> np.ndarray:
    """Apply colormap + data mask to a rescaled ImageData, returning an (H, W, 4) RGBA array.

    Mirrors what ImageData.render() does internally but stops before the PNG encode.
    Used so the antimeridian composite path can merge numpy arrays directly and encode
    only once at the end — previously each part was encoded to PNG, then re-decoded,
    composited, and re-encoded.
    """
    rgb, cmap_alpha = apply_cmap(img.data, cm)  # rgb: (3, H, W), cmap_alpha: (H, W)
    rgba = np.empty((rgb.shape[1], rgb.shape[2], 4), dtype=np.uint8)
    rgba[..., 0] = rgb[0]
    rgba[..., 1] = rgb[1]
    rgba[..., 2] = rgb[2]
    # Combine the colormap's own alpha (e.g. transparent categories) with the
    # ImageData mask (which marks NaN / out-of-extent pixels). Both are uint8 0/255.
    rgba[..., 3] = np.minimum(cmap_alpha, img.mask.astype(np.uint8))
    return rgba


def _img_to_rgba_categorical(img: ImageData, lut: dict[int, RGBA]) -> np.ndarray:
    """Discrete value→colour lookup for a categorical tile, returning (H, W, 4) RGBA.

    Unlike `_img_to_rgba` there is no rescale: the (nearest-resampled) data values are
    the raw integer flag codes, cast to uint8 so they index the LUT directly. Casting
    NaN to 0 is safe — those pixels are zeroed out by `img.mask` in the alpha channel.
    """
    codes = np.nan_to_num(img.data, nan=0.0).astype(np.uint8)  # (1, H, W)
    rgb, cmap_alpha = apply_cmap(codes, lut)
    rgba = np.empty((rgb.shape[1], rgb.shape[2], 4), dtype=np.uint8)
    rgba[..., 0] = rgb[0]
    rgba[..., 1] = rgb[1]
    rgba[..., 2] = rgb[2]
    # We skip img.rescale() on this path, so img.mask is still float (with NaN for
    # nodata). Derive validity from the data directly: a pixel is opaque only where
    # it had real data AND its category colour is opaque (transparent "none" stays clear).
    valid = (~np.isnan(img.data[0])).astype(np.uint8) * 255
    rgba[..., 3] = np.minimum(cmap_alpha, valid)
    return rgba


def _composite_over(base: np.ndarray, top: np.ndarray) -> np.ndarray:
    """Paint `top`'s opaque pixels over `base` (in place) and return it.

    Used to merge antimeridian-split segments — each segment covers a disjoint
    region, so a simple alpha>0 overwrite is sufficient (no blending needed).
    """
    mask = top[..., 3] > 0
    base[mask] = top[mask]
    return base


def _categorical_composite(
    parts: list[xr.DataArray],
    lut: dict[int, RGBA],
    read: Callable[[XarrayReader], ImageData],
) -> np.ndarray | None:
    """Resample each part via ``read`` through the discrete LUT, composite, return RGBA.

    Shared by the tile and bbox categorical paths — they differ only in the reader
    call (``.tile`` vs ``.part``). Returns None when no part intersects the request.
    """
    result: np.ndarray | None = None
    for da in parts:
        try:
            with XarrayReader(da) as reader:
                img = read(reader)
        except (TileOutsideBounds, NoDataInBounds):
            continue
        rgba = _img_to_rgba_categorical(img, lut)
        result = rgba if result is None else _composite_over(result, rgba)
    return result


def _validate_categorical_request(
    variable: str,
    attrs: Mapping[str, Any],
    colormap_name: str | None,
    fmt: str,
    *,
    rescale: tuple[float, float] | None = None,
    animated: bool = False,
) -> None:
    """Single gate for every categorical request rule, run before rendering.

    Lives here (not in the router) because the variable's ``attrs`` — the only way
    to know whether it's categorical — are already loaded for the render dispatch,
    so the checks cost no extra store read. Raises ``ValueError``; the router maps
    that to HTTP 400.

    Rules:
      * categorical variable → reject lossy (animated) WebP, which smears the hard
        category boundaries into spurious in-between colours;
      * categorical variable + ``rescale`` → reject (the discrete-lookup path has no
        continuous scale, so rescale silently does nothing — a client setting it has
        a misconception worth surfacing);
      * categorical variable + an explicit *continuous* colormap → reject (pass a
        categorical colormap, or omit it for the default palette);
      * categorical variable + a categorical colormap whose values ≠ flag_values →
        reject (its colours would map to the wrong codes, silently);
      * continuous variable + a categorical colormap → reject (its fixed colour
        slots are meaningless on the scale-dependent ramp path).
    """
    colormap_is_categorical = bool(colormap_name) and is_categorical(colormap_name)

    if not is_categorical_variable(attrs):
        if colormap_is_categorical:
            raise ValueError(
                f"Categorical colormap '{colormap_name}' can only be applied to a categorical "
                f"variable (one with CF flag_values); variable '{variable}' is continuous."
            )
        return

    if rescale is not None:
        raise ValueError(
            f"Variable '{variable}' is categorical; rescale does not apply (categories are "
            f"discrete codes, not a continuous scale). Omit rescale."
        )

    if fmt == "webp":
        kind = "animated WebP" if animated else "WebP"
        alternatives = "Use .apng or .gif." if animated else "Use .png."
        raise ValueError(
            f"Variable '{variable}' is categorical and cannot be encoded as {kind} "
            f"(lossy compression corrupts the discrete category boundaries). {alternatives}"
        )

    if colormap_name is not None and not colormap_is_categorical:
        raise ValueError(
            f"Variable '{variable}' is categorical; colormap '{colormap_name}' is a continuous "
            f"colormap. Pass a categorical colormap, or omit it to use the default palette."
        )

    if colormap_is_categorical:
        assert colormap_name is not None  # narrowed by colormap_is_categorical
        expected = sorted(int(v) for v in attrs["flag_values"])
        cmap_values = get_category_values(colormap_name)
        if cmap_values != expected:
            raise ValueError(
                f"Categorical colormap '{colormap_name}' covers values {cmap_values}, which do "
                f"not match variable '{variable}' flag_values {expected}."
            )


def _apply_crs(da: xr.DataArray) -> xr.DataArray:
    return da.rio.write_crs("EPSG:4326").rio.set_spatial_dims(x_dim="lon", y_dim="lat")


def _to_scalar_parts(ds: xr.Dataset, variable: str) -> list[xr.DataArray]:
    """Return float32 DataArrays ready for XarrayReader.

    Returns one element in the common case. Returns two elements when the data
    straddles the antimeridian (e.g. GSLA: 57–185°E):
      - primary:  lon < 180  (unchanged)
      - minor:    lon > 180  shifted by −360  (e.g. 180.2–185 → −179.8 to −175)

    Detection uses a contiguity check on the normalised coordinate array rather
    than a heuristic threshold: if wrapping lon > 180 to negative values leaves a
    gap larger than 2× the native resolution, the data is a regional straddle, not
    a global periodic grid.

    lon == 180 is excluded from both segments so that rioxarray's half-pixel padding
    keeps each segment's bounds strictly inside the ±180 limit rio_tiler enforces.
    """
    da = ds[variable].astype(np.float32)

    lat_min, lat_max = float(da.lat.min()), float(da.lat.max())
    lon_min, lon_max = float(da.lon.min()), float(da.lon.max())
    if not (-90 <= lat_min and lat_max <= 90 and -180 <= lon_min and lon_max <= 360):
        raise ValueError(
            f"Dataset '{variable}' does not appear to be in EPSG:4326: "
            f"lat [{lat_min:.1f}, {lat_max:.1f}], lon [{lon_min:.1f}, {lon_max:.1f}]. "
            "Expected lat ∈ [−90, 90] and lon ∈ [−180, 360]."
        )

    if float(da.lon.max()) > 180:
        normalised = np.where(da.lon.values > 180, da.lon.values - 360, da.lon.values)
        native_res = abs(float(da.lon.values[1] - da.lon.values[0]))
        max_gap = float(np.max(np.diff(np.sort(normalised))))

        if max_gap <= 2 * native_res:
            # Contiguous after normalisation → global-style wrap is safe.
            da = da.assign_coords(lon=("lon", normalised)).sortby("lon")
            return [_apply_crs(da)]

        # Antimeridian straddle: split into two contiguous segments.
        # Exclude exactly lon=180 from both sides — its half-pixel bound would
        # land at ±180.x, which exceeds rio_tiler's strict ±180 check.
        primary = _apply_crs(da.sel(lon=da.lon[da.lon < 180]))
        minor_da = da.sel(lon=da.lon[da.lon > 180])
        minor_da = _apply_crs(
            minor_da.assign_coords(lon=("lon", minor_da.lon.values - 360)).sortby("lon")
        )
        return [_apply_crs(primary), minor_da]

    return [_apply_crs(da)]


def _rescale_range(
    parts: list[xr.DataArray],
    rescale: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """Return (vmin, vmax) from rescale arg or data range; None if no valid data."""
    if rescale is not None:
        return rescale
    all_valid = np.concatenate([p.values[~np.isnan(p.values)].ravel() for p in parts])
    if not all_valid.size:
        return None
    return float(all_valid.min()), float(all_valid.max())


def render_tile(
    ds: xr.Dataset,
    variable: str,
    x: int,
    y: int,
    z: int,
    colormap_name: str | None = None,
    rescale: tuple[float, float] | None = None,
    fmt: ImageFormat = "png",
) -> bytes:
    """Return a 256×256 Web Mercator tile encoded as ``fmt``.

    Returns a fully transparent tile for tiles outside the data extent. Categorical
    variables (CF ``flag_values``) take the discrete-lookup path — nearest-neighbour
    resampling and a value-indexed LUT, no rescale; see [[colormap.categorical]].
    ``colormap_name`` None means "unspecified" (default viridis ramp / default
    categorical palette).
    """
    attrs = ds[variable].attrs
    _validate_categorical_request(variable, attrs, colormap_name, fmt, rescale=rescale)
    parts = _to_scalar_parts(ds, variable)

    if is_categorical_variable(attrs):
        scheme = resolve_scheme(attrs, colormap_name)
        result = _categorical_composite(
            parts, scheme.lut(), lambda r: r.tile(x, y, z, reproject_method="nearest")
        )
        return encode_rgba(result, fmt) if result is not None else empty_tile(fmt)

    vrange = _rescale_range(parts, rescale)
    if vrange is None:
        return empty_tile(fmt)
    vmin, vmax = vrange
    span = vmax - vmin or 1.0
    cm = resolve_colormap(colormap_name or "viridis")
    result: np.ndarray | None = None

    for da in parts:
        try:
            with XarrayReader(da) as reader:
                img = reader.tile(x, y, z, reproject_method="bilinear")
        except TileOutsideBounds:
            continue
        img.rescale(in_range=[(vmin, vmin + span)])
        rgba = _img_to_rgba(img, cm)
        if result is None:
            result = rgba
        else:
            mask = rgba[..., 3] > 0
            result[mask] = rgba[mask]

    return encode_rgba(result, fmt) if result is not None else empty_tile(fmt)


def _bbox_parts_to_rgba(
    parts: list[xr.DataArray],
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
    vmin: float,
    span: float,
    cm: dict[int, tuple[int, int, int, int]],
) -> np.ndarray | None:
    """Resample each antimeridian-split part into the bbox, composite, return RGBA.

    Returns None when the bbox intersects none of the parts (caller decides whether
    to emit a transparent placeholder or skip).
    """
    lon_min, lat_min, lon_max, lat_max = bbox_wgs84
    result: np.ndarray | None = None
    for da in parts:
        try:
            with XarrayReader(da) as reader:
                img = reader.part(
                    (lon_min, lat_min, lon_max, lat_max),
                    width=width,
                    height=height,
                    reproject_method="bilinear",
                )
        except (TileOutsideBounds, NoDataInBounds):
            continue
        img.rescale(in_range=[(vmin, vmin + span)])
        rgba = _img_to_rgba(img, cm)
        if result is None:
            result = rgba
        else:
            mask = rgba[..., 3] > 0
            result[mask] = rgba[mask]
    return result


def render_bbox(
    ds: xr.Dataset,
    variable: str,
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    colormap_name: str | None = None,
    rescale: tuple[float, float] | None = None,
    crs: str = "EPSG:4326",
    fmt: ImageFormat = "png",
) -> bytes:
    """Return an image for an arbitrary bbox encoded as ``fmt``.

    bbox must be (minx, miny, maxx, maxy) in the given crs ('EPSG:4326' degrees or 'EPSG:3857' meters).
    Returns a fully transparent tile when the bbox does not intersect the data. Categorical
    variables take the discrete-lookup path (see [[colormap.categorical]] / `render_tile`).
    """
    attrs = ds[variable].attrs
    _validate_categorical_request(variable, attrs, colormap_name, fmt, rescale=rescale)
    parts = _to_scalar_parts(ds, variable)
    bbox_wgs84 = bbox_to_wgs84(bbox, crs)
    lo, la_min, hi, la_max = bbox_wgs84

    if is_categorical_variable(attrs):
        scheme = resolve_scheme(attrs, colormap_name)
        result = _categorical_composite(
            parts,
            scheme.lut(),
            lambda r: r.part(
                (lo, la_min, hi, la_max), width=width, height=height, reproject_method="nearest"
            ),
        )
        return encode_rgba(result, fmt) if result is not None else empty_tile(fmt)

    vrange = _rescale_range(parts, rescale)
    if vrange is None:
        return empty_tile(fmt)
    vmin, vmax = vrange
    span = vmax - vmin or 1.0
    cm = resolve_colormap(colormap_name or "viridis")

    result = _bbox_parts_to_rgba(parts, bbox_wgs84, width, height, vmin, span, cm)
    return encode_rgba(result, fmt) if result is not None else empty_tile(fmt)


def render_bbox_animation(
    datasets: list[xr.Dataset],
    variable: str,
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    colormap_name: str | None = None,
    rescale: tuple[float, float] | None = None,
    crs: str = "EPSG:4326",
    fmt: AnimatedFormat = "webp",
    duration_ms: int = 200,
) -> bytes:
    """Render the same bbox across ``datasets`` and assemble as an animated image.

    When ``rescale`` is None, vmin/vmax is computed across the union of every frame's
    data so the colour ramp stays stable from frame to frame; computing it per-frame
    causes flicker in low-variance areas.
    Frames whose data does not intersect the bbox are emitted as fully transparent
    so timing stays aligned with the date sequence. Categorical variables take the
    discrete-lookup path (nearest resampling, value-indexed LUT, no rescale).
    """
    if not datasets:
        raise ValueError("render_bbox_animation requires at least one dataset")

    attrs = datasets[0][variable].attrs
    _validate_categorical_request(
        variable, attrs, colormap_name, fmt, rescale=rescale, animated=True
    )

    parts_per_frame = [_to_scalar_parts(ds, variable) for ds in datasets]
    bbox_wgs84 = bbox_to_wgs84(bbox, crs)

    if is_categorical_variable(attrs):
        lut = resolve_scheme(attrs, colormap_name).lut()
        lo, la_min, hi, la_max = bbox_wgs84

        def _read_part(r: XarrayReader) -> ImageData:
            return r.part(
                (lo, la_min, hi, la_max), width=width, height=height, reproject_method="nearest"
            )

        cat_frames: list[np.ndarray] = []
        for parts in parts_per_frame:
            rgba = _categorical_composite(parts, lut, _read_part)
            if rgba is None:
                rgba = np.zeros((height, width, 4), dtype=np.uint8)
            cat_frames.append(rgba)
        return encode_rgba_animation(cat_frames, fmt, duration_ms)

    if rescale is not None:
        vmin, vmax = rescale
    else:
        all_parts = [p for parts in parts_per_frame for p in parts]
        vrange = _rescale_range(all_parts, None)
        if vrange is None:
            empty = np.zeros((height, width, 4), dtype=np.uint8)
            return encode_rgba_animation([empty] * len(datasets), fmt, duration_ms)
        vmin, vmax = vrange

    span = vmax - vmin or 1.0
    cm = resolve_colormap(colormap_name or "viridis")

    frames: list[np.ndarray] = []
    for parts in parts_per_frame:
        rgba = _bbox_parts_to_rgba(parts, bbox_wgs84, width, height, vmin, span, cm)
        if rgba is None:
            rgba = np.zeros((height, width, 4), dtype=np.uint8)
        frames.append(rgba)

    return encode_rgba_animation(frames, fmt, duration_ms)
