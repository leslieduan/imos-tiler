import logging
import threading

import xarray as xr
from cachetools import LRUCache

from constants import COORD_NAMES, Product

logger = logging.getLogger(__name__)

# Dataset stores opened once per URL and reused across requests.
_stores: dict[str, xr.Dataset] = {}
_store_lock = threading.Lock()

# Separate lock for product.lod_grids lazy initialization.
_lod_grids_lock = threading.Lock()

# Cache of fully-computed 2D (lat × lon) slices keyed by (store_url, date, variables).
# Each slice is ~7 MB (4 variables × 351 × 641 × float64); maxsize=20 ≈ 140 MB.
_slice_cache: LRUCache = LRUCache(maxsize=20)
_slice_lock = threading.Lock()


def _get_store(store_url: str) -> xr.Dataset:
    with _store_lock:
        if store_url not in _stores:
            ds = xr.open_zarr(store_url, storage_options={"anon": True})
            # The rename itself is also essentially free — xarray's .rename() only touches coordinate metadata, no data is loaded into memory. Same for .sortby() on a store — it rearranges the index, not the array data
            rename = {k: v for k, v in COORD_NAMES.items() if k in ds.dims or k in ds.coords}
            if rename:
                ds = ds.rename(rename)
            if "lat" not in ds.dims or "lon" not in ds.dims:
                raise ValueError(
                    f"Store {store_url!r} missing lat/lon dims after rename "
                    f"(found: {list(ds.dims)})"
                )
            if "time" in ds.dims:
                ds = ds.sortby("time")
            _stores[store_url] = ds
    return _stores[store_url]


def get_lod_grids(product: Product) -> dict[int, tuple[int, int]]:
    """
    Ensure product.lod_grids is populated from actual store dimensions, then return it.
    Writes back to product on first call so subsequent callers find it already set.
    Double-checked locking: fast path avoids lock overhead on every warm call.
    """
    if product.lod_grids:
        return product.lod_grids

    with _lod_grids_lock:
        if product.lod_grids:
            return product.lod_grids

        store = _get_store(product.source_path)
        data_height = store.sizes["lat"]
        data_width = store.sizes["lon"]
        product.apply_computed_lod_grids(data_width, data_height)

    return product.lod_grids


def load_slice(store_url: str, date: str, variables: list[str]) -> xr.Dataset:
    """
    Return a fully-computed 2D (lat × lon) slice for the given store, date, and variables.
    Uses nearest-match on time so callers don't need to know exact timestamps.
    Coordinate names are already normalised by _get_store.
    """
    cache_key = (store_url, date, tuple(sorted(variables)))
    with _slice_lock:
        cached = _slice_cache.get(cache_key)
        if cached is not None:
            return cached

    store = _get_store(store_url)
    try:
        ds = store[variables].sel(time=date, method="nearest").compute()
    except KeyError as e:
        raise FileNotFoundError(f"No data found near date {date}") from e

    with _slice_lock:
        _slice_cache[cache_key] = ds
    return ds
