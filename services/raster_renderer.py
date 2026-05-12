import io
from functools import lru_cache

import numpy as np
import xarray as xr
from PIL import Image
from rio_tiler.colormap import cmap as _rio_cmap
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io.xarray import XarrayReader

TILE_SIZE = 256


@lru_cache(maxsize=64)
def _colormap(name: str) -> dict[int, tuple[int, int, int, int]]:
    """Return a rio-tiler colormap dict for the given name.

    Tries rio-tiler's built-in colormaps first, then falls back to matplotlib
    so that diverging colormaps like RdBu_r are also available.
    """
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


def empty_png() -> bytes:
    """Fully transparent 256×256 PNG — returned for tiles outside the data extent."""
    buf = io.BytesIO()
    Image.fromarray(np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8), "RGBA").save(
        buf, format="PNG", optimize=False
    )
    return buf.getvalue()


def _to_scalar(ds: xr.Dataset, variable: str | list[str]) -> xr.DataArray:
    """Return a 2-D (lat × lon) float32 DataArray ready for XarrayReader.

    UV products are collapsed to current speed so a single scalar is rendered.
    """
    if isinstance(variable, list):
        u = ds[variable[0]].values.astype(np.float32)
        v = ds[variable[1]].values.astype(np.float32)
        speed = np.sqrt(u**2 + v**2)
        da = xr.DataArray(speed, coords=ds[variable[0]].coords, dims=ds[variable[0]].dims)
    else:
        da = ds[variable].astype(np.float32)

    # Some stores use 0–360 longitude convention (values > 180).
    # rioxarray requires -180 to 180, so wrap and re-sort before handing to XarrayReader.
    if float(da.lon.max()) > 180:
        da = da.assign_coords(lon=((da.lon + 180) % 360) - 180)
        da = da.sortby("lon")

    da = da.rio.write_crs("EPSG:4326")
    da = da.rio.set_spatial_dims(x_dim="lon", y_dim="lat")
    return da


def render_raster_tile(
    ds: xr.Dataset,
    variable: str | list[str],
    x: int,
    y: int,
    z: int,
    colormap_name: str = "viridis",
    rescale: tuple[float, float] | None = None,
) -> bytes:
    """Return a 256×256 Web Mercator PNG tile.

    Returns a fully transparent tile for tiles outside the data extent.
    """
    da = _to_scalar(ds, variable)

    if rescale is None:
        valid = da.values[~np.isnan(da.values)]
        if not valid.size:
            return empty_png()
        vmin, vmax = float(valid.min()), float(valid.max())
    else:
        vmin, vmax = rescale

    try:
        with XarrayReader(da) as reader:
            img = reader.tile(x, y, z, reproject_method="bilinear")
    except TileOutsideBounds:
        return empty_png()

    span = vmax - vmin or 1.0
    img.rescale(in_range=[(vmin, vmin + span)])
    return img.render(img_format="PNG", colormap=_colormap(colormap_name))
