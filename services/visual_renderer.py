import io
from functools import lru_cache

import numpy as np
import xarray as xr
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from rio_tiler.colormap import cmap as _rio_cmap
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io.xarray import XarrayReader
from rioxarray.exceptions import NoDataInBounds

from services.colormap_store import get_colormap, is_categorical

_mercator_to_wgs84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

TILE_SIZE = 256


@lru_cache(maxsize=64)
def _colormap(name: str) -> dict[int, tuple[int, int, int, int]]:
    """Return a rio-tiler colormap dict for the given name.

    Checks custom colormaps first, then rio-tiler's built-ins, then matplotlib
    so that diverging colormaps like RdBu_r are also available.
    """
    entries = get_colormap(name)
    if entries is not None:
        if len(entries) != 256:
            raise ValueError(
                f"Custom colormap {name!r} must have exactly 256 entries, got {len(entries)}"
            )
        return {i: entries[i] for i in range(256)}
    try:
        return _rio_cmap.get(name)
    except Exception:
        pass
    import matplotlib

    try:
        cm = matplotlib.colormaps[name]
    except KeyError as exc:
        raise ValueError(f"Unknown colormap: {name!r}") from exc
    rgba = (cm(np.linspace(0, 1, 256)) * 255).astype(np.uint8)
    return {
        i: (int(rgba[i, 0]), int(rgba[i, 1]), int(rgba[i, 2]), int(rgba[i, 3])) for i in range(256)
    }


def _build_empty_png() -> bytes:
    """Encode a fully transparent 256×256 RGBA PNG. Called once at module load."""
    buf = io.BytesIO()
    Image.fromarray(np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8), "RGBA").save(
        buf, format="PNG", optimize=False
    )
    return buf.getvalue()


# Returned for tiles outside the data extent. Bytes are immutable, so a single
# instance is safe to reuse across all responses — no need to re-encode per call.
_EMPTY_PNG = _build_empty_png()


def empty_png() -> bytes:
    return _EMPTY_PNG


def _composite_png(base: bytes, overlay: bytes) -> bytes:
    """Merge two same-size RGBA PNGs: non-transparent overlay pixels replace base pixels."""
    base_arr = np.array(Image.open(io.BytesIO(base)).convert("RGBA"))
    over_arr = np.array(Image.open(io.BytesIO(overlay)).convert("RGBA"))
    mask = over_arr[..., 3] > 0
    base_arr[mask] = over_arr[mask]
    buf = io.BytesIO()
    Image.fromarray(base_arr, "RGBA").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


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
    colormap_name: str = "viridis",
    rescale: tuple[float, float] | None = None,
) -> bytes:
    """Return a 256×256 Web Mercator PNG tile.

    Returns a fully transparent tile for tiles outside the data extent.
    """
    parts = _to_scalar_parts(ds, variable)
    vrange = _rescale_range(parts, rescale)
    if vrange is None:
        return empty_png()
    vmin, vmax = vrange
    span = vmax - vmin or 1.0
    cm = _colormap(colormap_name)
    result: bytes | None = None

    for da in parts:
        try:
            with XarrayReader(da) as reader:
                img = reader.tile(x, y, z, reproject_method="bilinear")
        except TileOutsideBounds:
            continue
        img.rescale(in_range=[(vmin, vmin + span)])
        rendered = img.render(img_format="PNG", colormap=cm)
        result = rendered if result is None else _composite_png(result, rendered)

    return result or empty_png()


def render_bbox(
    ds: xr.Dataset,
    variable: str,
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    colormap_name: str = "viridis",
    rescale: tuple[float, float] | None = None,
    crs: str = "EPSG:4326",
) -> bytes:
    """Return a PNG image for an arbitrary bbox.

    bbox must be (minx, miny, maxx, maxy) in the given crs ('EPSG:4326' degrees or 'EPSG:3857' meters).
    Returns a fully transparent tile when the bbox does not intersect the data.
    """
    parts = _to_scalar_parts(ds, variable)
    vrange = _rescale_range(parts, rescale)
    if vrange is None:
        return empty_png()
    vmin, vmax = vrange
    span = vmax - vmin or 1.0
    cm = _colormap(colormap_name)

    minx, miny, maxx, maxy = bbox
    if crs == "EPSG:3857":
        lon_min, lat_min = _mercator_to_wgs84.transform(minx, miny)
        lon_max, lat_max = _mercator_to_wgs84.transform(maxx, maxy)
    else:
        lon_min, lat_min, lon_max, lat_max = minx, miny, maxx, maxy

    result: bytes | None = None

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
        rendered = img.render(img_format="PNG", colormap=cm)
        result = rendered if result is None else _composite_png(result, rendered)

    return result or empty_png()


def render_legend(
    colormap_name: str,
    rescale: tuple[float, float] | None = None,
    width: int = 256,
    height: int = 40,
    orientation: str = "horizontal",
) -> bytes:
    """Render a linear color legend PNG for the given colormap.

    The color bar fills the image. If rescale=(lo, hi) is provided, tick labels
    at lo, midpoint, and hi are drawn alongside the bar.

    For categorical colormaps the bar shows discrete equal-width color blocks
    (one per registered category) rather than a smooth gradient.
    """
    cm = _colormap(colormap_name)
    categorical = is_categorical(colormap_name)
    lut = np.array([cm[i] for i in range(256)], dtype=np.uint8)
    has_labels = rescale is not None
    LABEL_PX = 20  # pixels reserved alongside the bar for tick labels

    if orientation == "horizontal":
        bar_h = max(1, height - LABEL_PX) if has_labels else height
        bar_w = width
        bar = _build_colorbar(lut, categorical, bar_w, bar_h, vertical=False)
        canvas = np.zeros((height, width, 4), dtype=np.uint8)
        canvas[:bar_h, :] = bar
        if has_labels:
            canvas[bar_h:, :] = [255, 255, 255, 255]
        img = Image.fromarray(canvas, "RGBA")
        if has_labels:
            _draw_h_labels(img, rescale, bar_h, width)  # type: ignore[arg-type]
    else:
        bar_w = max(1, width - LABEL_PX) if has_labels else width
        bar_h = height
        bar = _build_colorbar(lut, categorical, bar_w, bar_h, vertical=True)
        canvas = np.zeros((height, width, 4), dtype=np.uint8)
        canvas[:, :bar_w] = bar
        if has_labels:
            canvas[:, bar_w:] = [255, 255, 255, 255]
        img = Image.fromarray(canvas, "RGBA")
        if has_labels:
            _draw_v_labels(img, rescale, bar_w, height)  # type: ignore[arg-type]

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _build_colorbar(
    lut: np.ndarray,
    categorical: bool,
    bar_w: int,
    bar_h: int,
    vertical: bool,
) -> np.ndarray:
    """Return a (bar_h, bar_w, 4) uint8 color bar array."""
    bar = np.zeros((bar_h, bar_w, 4), dtype=np.uint8)
    if categorical:
        active = [lut[i] for i in range(256) if lut[i, 3] > 0]
        n = len(active)
        if n:
            dim = bar_h if vertical else bar_w
            for idx, color in enumerate(active):
                d0 = idx * dim // n
                d1 = (idx + 1) * dim // n if idx < n - 1 else dim
                if vertical:
                    bar[d0:d1, :] = color
                else:
                    bar[:, d0:d1] = color
    elif vertical:
        indices = np.round(np.linspace(255, 0, bar_h)).astype(int)
        bar[:] = lut[indices][:, np.newaxis, :]
    else:
        indices = np.round(np.linspace(0, 255, bar_w)).astype(int)
        bar[:] = lut[indices][np.newaxis, :, :]
    return bar


def _draw_h_labels(
    img: Image.Image,
    rescale: tuple[float, float],
    bar_h: int,
    width: int,
) -> None:
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=11)
    lo, hi = rescale
    ticks = [(lo, 0), ((lo + hi) / 2, width // 2), (hi, width - 1)]
    for val, x in ticks:
        draw.line([(x, bar_h), (x, bar_h + 3)], fill=(80, 80, 80, 255))
        label = f"{val:.4g}"
        bbox = font.getbbox(label)
        lw = bbox[2] - bbox[0]
        tx = max(0, min(x - lw // 2, width - lw))
        draw.text((tx, bar_h + 4), label, fill=(0, 0, 0, 255), font=font)


def _draw_v_labels(
    img: Image.Image,
    rescale: tuple[float, float],
    bar_w: int,
    height: int,
) -> None:
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=11)
    lo, hi = rescale
    # vertical: top = hi, bottom = lo
    ticks = [(hi, 0), ((lo + hi) / 2, height // 2), (lo, height - 1)]
    for val, y in ticks:
        draw.line([(bar_w, y), (bar_w + 3, y)], fill=(80, 80, 80, 255))
        label = f"{val:.4g}"
        bbox = font.getbbox(label)
        lh = bbox[3] - bbox[1]
        ty = max(0, min(y - lh // 2, height - lh))
        draw.text((bar_w + 4, ty), label, fill=(0, 0, 0, 255), font=font)
