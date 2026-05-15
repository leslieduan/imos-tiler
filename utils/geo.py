"""Pure geospatial utility functions shared across renderer modules."""

import math

import xarray as xr


def json_safe_float(v) -> float | None:
    """Return float(v), or None if v is NaN or infinite (not JSON-serialisable)."""
    f = float(v)
    return None if math.isnan(f) or math.isinf(f) else f


def dataset_bounds(ds: xr.Dataset) -> tuple[float, float, float, float]:
    """Return (lon_min, lon_max, lat_min, lat_max) from a dataset's coordinate arrays."""
    return (
        float(ds.lon.min().values),
        float(ds.lon.max().values),
        float(ds.lat.min().values),
        float(ds.lat.max().values),
    )
