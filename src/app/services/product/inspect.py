"""Introspect a product's underlying Zarr store: dimensions, per-variable chunk
shape / dtype, and dataset + variable attributes.

Pure introspection like manifest.py — takes a product and its dataset and returns
a JSON-safe description, no rendering or caching. Unlike the manifest (which
describes a single date's slice), this operates on the *full* store dataset, so
the reported dimensions include the time axis and the chunk shapes reflect the
on-disk Zarr layout rather than a one-timestep view.
"""

import math
from typing import Any

import numpy as np
import xarray as xr

from app.services.product.product import Product


def _json_safe(value: Any) -> Any:
    """Recursively coerce numpy scalars/arrays and non-finite floats into JSON-safe
    Python values, so dataset attributes (often numpy-typed) survive serialisation."""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, np.ndarray):
        return [_json_safe(v) for v in value.tolist()]
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return value


def _chunk_shape(da: xr.DataArray) -> list[int] | None:
    """Native Zarr chunk shape in dim order, or None if the array is unchunked.

    Prefers `encoding` (the exact on-disk shape recorded by zarr) and falls back to
    the dask block layout — with chunks={} the first block of each dim equals the
    native zarr chunk size (see services/store/registry._open_store)."""
    chunks = da.encoding.get("chunks") or da.encoding.get("preferred_chunks")
    if isinstance(chunks, dict):  # preferred_chunks is keyed by dim name
        return [int(chunks[d]) for d in da.dims]
    if chunks:
        return [int(c) for c in chunks]
    if da.chunks is not None:
        return [int(blocks[0]) for blocks in da.chunks]
    return None


def inspect_product(product: Product, ds: xr.Dataset) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    for var in product.variables:
        da = ds[var]
        variables[var] = {
            "dimensions": [str(d) for d in da.dims],
            "shape": [int(s) for s in da.shape],
            "dtype": str(da.dtype),
            "chunks": _chunk_shape(da),
            "units": da.attrs.get("units"),
            "attributes": _json_safe(dict(da.attrs)),
        }
    return {
        "id": product.id,
        "source_path": product.source_path,
        "dimensions": {str(k): int(v) for k, v in ds.sizes.items()},
        "variables": variables,
        "attributes": _json_safe(dict(ds.attrs)),
    }
