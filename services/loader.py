import concurrent.futures
import logging
import os
import threading

import xarray as xr
from cachetools import LRUCache

from constants import COORD_NAMES, Product

logger = logging.getLogger(__name__)

# Dataset stores opened once per URL and reused across requests.
_stores: dict[str, xr.Dataset] = {}
_store_lock = threading.Lock()
_store_in_flight: dict[str, concurrent.futures.Future] = {}

# Separate lock for product.lod_grids lazy initialization.
_lod_grids_lock = threading.Lock()

# Cache of fully-computed 2D (lat × lon) slices keyed by (store_url, date, variables).
# Each slice is ~7 MB (4 variables × 351 × 641 × float64).
# SLICE_CACHE_SIZE controls how many slices to hold in memory (default 50 ≈ 350 MB).
# Raise this as you add products/dates; lower it on memory-constrained deployments.
_SLICE_CACHE_SIZE = int(os.environ.get("SLICE_CACHE_SIZE", 50))
_slice_cache: LRUCache = LRUCache(maxsize=_SLICE_CACHE_SIZE)
_slice_lock = threading.Lock()
_slice_in_flight: dict[tuple, concurrent.futures.Future] = {}


# Fix: serialised store opens under _store_lock meant two requests for *different* store URLs
# arriving before either had finished opening would block each other unnecessarily — the second
# request had to wait for the first store's full xr.open_zarr() even though they were unrelated.
# Resolution: replace the single global lock with a per-URL Future in _store_in_flight. The first
# thread to request a URL creates the Future and does the open; any other thread requesting the
# same URL concurrently waits on that same Future instead of blocking all other URLs too.
def _get_store(store_url: str) -> xr.Dataset:
    # Fast path: already open.
    should_open = False
    with _store_lock:
        if store_url in _stores:
            return _stores[store_url]
        if store_url in _store_in_flight:
            future = _store_in_flight[store_url]
        else:
            future: concurrent.futures.Future = concurrent.futures.Future()
            _store_in_flight[store_url] = future
            should_open = True

    if not should_open:
        return future.result()

    try:
        ds = xr.open_zarr(store_url, storage_options={"anon": True})
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
        with _store_lock:
            _stores[store_url] = ds
        future.set_result(ds)
    except Exception as e:
        future.set_exception(e)
        raise
    finally:
        with _store_lock:
            _store_in_flight.pop(store_url, None)
    return ds


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


def get_available_dates(store_url: str) -> list[str]:
    store = _get_store(store_url)
    if "time" not in store.dims:
        return []
    return [str(t)[:10] for t in store.coords["time"].values]


# Fix: cache stampede — the old code released _slice_lock immediately after a cache miss, then
# called .compute() outside the lock. Any request arriving during that S3 download window (up to
# several seconds) would also see a cache miss and launch its own redundant .compute(), wasting
# bandwidth and memory for identical data.
# Resolution: _slice_in_flight tracks an in-progress Future per cache key. The first thread to
# miss the cache creates the Future and does the .compute(); all other threads that arrive for the
# same key while it is in flight skip .compute() and block on future.result() instead, receiving
# the same result when the single download completes. Errors propagate to all waiting threads so a
# failed request does not permanently block future attempts for the same key.
def load_slice(store_url: str, date: str, variables: list[str]) -> xr.Dataset:
    """
    Return a fully-computed 2D (lat × lon) slice for the given store, date, and variables.
    Uses nearest-match on time so callers don't need to ask exact timestamps.
    Coordinate names are already normalised by _get_store.
    """
    cache_key = (store_url, date, tuple(sorted(variables)))

    # Fast path: already cached.
    should_compute = False
    with _slice_lock:
        cached = _slice_cache.get(cache_key)
        if cached is not None:
            return cached
        if cache_key in _slice_in_flight:
            future = _slice_in_flight[cache_key]
        else:
            future: concurrent.futures.Future = concurrent.futures.Future()
            _slice_in_flight[cache_key] = future
            should_compute = True

    if not should_compute:
        return future.result()

    try:
        store = _get_store(store_url)
        ds = store[variables].sel(time=date, method="nearest").compute()
        with _slice_lock:
            _slice_cache[cache_key] = ds
        future.set_result(ds)
    except KeyError as e:
        exc = FileNotFoundError(f"No data found near date {date}")
        future.set_exception(exc)
        raise exc from e
    except Exception as e:
        future.set_exception(e)
        raise
    finally:
        with _slice_lock:
            _slice_in_flight.pop(cache_key, None)
    return ds
