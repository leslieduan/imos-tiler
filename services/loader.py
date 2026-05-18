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

import pandas as pd
import xarray as xr
from cachetools import LRUCache

from constants import Product
from services.disk_cache import (
    disk_cache_path,
    evict_product_dir,
    read_slice_from_disk,
)
from services.store_registry import get_store, store_registry
from utils.memoizer import Memoizer

logger = logging.getLogger(__name__)

_SLICE_CACHE_SIZE = int(os.environ.get("SLICE_CACHE_SIZE", 10))
_slice_cache: LRUCache = LRUCache(maxsize=_SLICE_CACHE_SIZE)
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

    def factory() -> xr.Dataset:
        cache_path = disk_cache_path(store_url, date, list(variables))
        if cache_path is not None and cache_path.exists():
            cached = read_slice_from_disk(cache_path)
            if cached is not None:
                return cached

        store = get_store(store_url)
        index = store_registry.date_index(store_url)
        matching = list(index.get(date, ()))
        if not matching:
            latest = max(index) if index else None
            hint = (
                f" Latest available date is {latest!r}." if latest else " No dates are available."
            )
            raise FileNotFoundError(f"No data for date {date!r}.{hint}")
        if len(matching) > 1:
            logger.warning(
                "Multiple timestamps (%d) map to date %r in %s; using first: %s",
                len(matching),
                date,
                store_url,
                matching[0],
            )
        try:
            return store[variables].sel(time=pd.Timestamp(matching[0])).compute()
        except KeyError as e:
            raise FileNotFoundError(f"No data found for date {date}") from e

    return _slice_memo.get_or_compute(cache_key, factory)


def slice_memo_stats() -> dict:
    """In-flight + LRU stats for the L2 slice memoizer. Used by /admin/cache."""
    return {
        **_slice_memo.stats(),
        "cache_size": len(_slice_cache),
        "cache_max": _slice_cache.maxsize,
    }


def evict_product_cache(product: Product) -> None:
    """Remove all in-memory and disk cache entries for a deleted product."""
    from services.data_renderer import evict_processed_cache

    vars_tuple = tuple(sorted(product.variables))

    removed = _slice_memo.evict_matching(
        lambda k: k[0] == product.source_path and k[2] == vars_tuple
    )
    if removed:
        logger.info("Memory cache evicted %d slice(s) for: %s", removed, product.id)

    evict_processed_cache(product)
    evict_product_dir(product)
