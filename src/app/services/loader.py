"""In-memory slice loading and cross-cutting product cache eviction.

Three responsibilities:
  * ``load_slice`` — return a fully-computed 2-D slice for a (store, date,
    variables) tuple. Cached in an LRU (L2); checks the L3 disk cache before
    falling through to S3. Concurrent identical requests share one compute via
    the slice Memoizer.
  * ``get_available_dates`` / ``get_lod_grids`` — small store-aware accessors
    used by routers; both touch the store registry on first call.
  * ``evict_product_cache`` — fan-out eviction across every layer (L2 slices,
    L1 processed grids, L3 disk dir) when a product is deregistered.

Long-lived store handles and disk-cache lifecycle live in their own modules
([[store_registry]], [[disk_cache]]).
"""

import logging
import os
import threading
import time

import pandas as pd
import xarray as xr
from cachetools import TTLCache

from app.constants import Product
from app.services.disk_cache import (
    disk_cache_path,
    evict_product_dir,
    read_slice_from_disk,
)
from app.services.store_registry import get_store, store_registry
from app.utils.memoizer import Memoizer

logger = logging.getLogger(__name__)

_SLICE_CACHE_SIZE = int(os.environ.get("SLICE_CACHE_SIZE", 10))
# L2 entries are useful for the lifetime of one active map view: a burst of tile
# requests for the same (product, date) lands within seconds, then the user moves
# on. TTL evicts entries that haven't been refreshed since insertion so idle RAM
# returns to baseline; maxsize still bounds peak capacity under burst pressure.
_SLICE_CACHE_TTL = int(os.environ.get("SLICE_CACHE_TTL_SECONDS", 600))
_SLOW_FETCH_THRESHOLD = float(os.environ.get("SLOW_FETCH_THRESHOLD_SECONDS", 5))
_slice_cache: TTLCache = TTLCache(maxsize=_SLICE_CACHE_SIZE, ttl=_SLICE_CACHE_TTL)
_slice_memo: Memoizer = Memoizer(_slice_cache)

# Separate lock for product.lod_grids lazy initialization (unrelated to store state).
_lod_grids_lock = threading.Lock()


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

        store = get_store(product.source_path)
        data_height = store.sizes["lat"]
        data_width = store.sizes["lon"]
        product.apply_computed_lod_grids(data_width, data_height)

    return product.lod_grids


def get_available_dates(store_url: str) -> list[str]:
    get_store(store_url)  # ensures the date index for this URL is populated
    index = store_registry.date_index(store_url)
    return sorted(index) if index else []


def _compute_slice_from_store(store_url: str, date: str, variables: list[str]) -> xr.Dataset:
    """Fetch a 2-D slice from L3 disk cache or fall through to the Zarr store.

    Read-only with respect to L3 — populating the disk cache is the prewarmer's
    job, not the request path's. Both `load_slice` and `load_slice_uncached`
    delegate here; they differ only in whether the result lands in L2.
    """
    cache_path = disk_cache_path(store_url, date, list(variables))
    if cache_path.exists():
        cached = read_slice_from_disk(cache_path)
        if cached is not None:
            return cached

    store = get_store(store_url)
    index = store_registry.date_index(store_url)
    matching = list(index.get(date, ()))
    if not matching:
        latest = max(index) if index else None
        hint = f" Latest available date is {latest!r}." if latest else " No dates are available."
        raise FileNotFoundError(f"No data for date {date!r}.{hint}")
    try:
        t0 = time.monotonic()
        result = store[variables].sel(time=pd.Timestamp(matching[0])).compute()
        elapsed = time.monotonic() - t0
        if elapsed > _SLOW_FETCH_THRESHOLD:
            logger.warning(
                "Slow S3 fetch",
                extra={"store_url": store_url, "date": date, "seconds": elapsed},
            )
        return result
    except KeyError as e:
        raise FileNotFoundError(f"No data found for date {date}") from e


# Concurrent identical requests share one S3 .compute() via _slice_memo: the first
# thread to miss runs the factory, the rest block on its Future. Errors propagate
# to all waiters and the in-flight entry is cleared, so a failed request never
# permanently blocks subsequent attempts for the same key.
def load_slice(store_url: str, date: str, variables: list[str]) -> xr.Dataset:
    """
    Return a fully-computed 2D (lat × lon) slice for the given store, date, and variables.
    Uses nearest-match on time so callers don't need to ask exact timestamps.
    Coordinate names are already normalised by the store registry.
    """
    cache_key = (store_url, date, tuple(sorted(variables)))
    return _slice_memo.get_or_compute(
        cache_key, lambda: _compute_slice_from_store(store_url, date, variables)
    )


def load_slice_uncached(store_url: str, date: str, variables: list[str]) -> xr.Dataset:
    """Return a 2-D slice without touching the L2 in-memory cache.

    Reads from the L3 disk cache if present, otherwise pulls directly from the
    Zarr store. Never writes to L3 — animation requests can span dates outside
    the prewarmed window, and we don't want a rare endpoint to pollute the
    shared disk cache or evict another product's hot slices.
    """
    return _compute_slice_from_store(store_url, date, variables)


def slice_memo_stats() -> dict:
    """In-flight + LRU stats for the L2 slice memoizer. Used by /admin/cache."""
    return {
        **_slice_memo.stats(),
        "cache_size": len(_slice_cache),
        "cache_max": _slice_cache.maxsize,
    }


def clear_slice_cache() -> int:
    """Drop every entry in the L2 slice cache. Returns count removed."""
    removed = _slice_memo.evict_matching(lambda _: True)
    if removed:
        logger.info("Memory cache cleared", extra={"slices_removed": removed})
    return removed


def evict_product_cache(product: Product) -> None:
    """Remove all in-memory and disk cache entries for a deleted product."""
    from app.services.data_renderer import evict_processed_cache

    vars_tuple = tuple(sorted(product.variables))

    removed = _slice_memo.evict_matching(
        lambda k: k[0] == product.source_path and k[2] == vars_tuple
    )
    if removed:
        logger.info(
            "Memory cache evicted for product",
            extra={"product_id": product.id, "slices_removed": removed},
        )

    evict_processed_cache(product)
    evict_product_dir(product)
