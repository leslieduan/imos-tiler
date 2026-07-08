"""In-memory slice loading (L2).

``load_slice`` returns a fully-computed 2-D slice for a (store, date,
variables) tuple. Cached in an LRU (L2); falls through to S3 on a miss.
Concurrent identical requests share one compute via the slice Memoizer.

Long-lived store handles live in their own module ([[store.registry]]).
"""

import logging
import os
import time

import pandas as pd
import xarray as xr
from cachetools import TTLCache

from app.services.caching.backend_factory import create_memoizer
from app.services.caching.memoizer import CacheBackend
from app.services.rendering.masks import apply_ocean_mask
from app.services.store.registry import get_store, store_registry

logger = logging.getLogger(__name__)

_SLICE_CACHE_SIZE = int(os.environ.get("SLICE_CACHE_SIZE", 10))
# L2 entries are useful for the lifetime of one active map view: a burst of tile
# requests for the same (product, date) lands within seconds, then the user moves
# on. TTL evicts entries that haven't been refreshed since insertion so idle RAM
# returns to baseline; maxsize still bounds peak capacity under burst pressure.
_SLICE_CACHE_TTL = int(os.environ.get("SLICE_CACHE_TTL_SECONDS", 600))
_SLOW_FETCH_THRESHOLD = float(os.environ.get("SLOW_FETCH_THRESHOLD_SECONDS", 5))
_slice_cache: TTLCache = TTLCache(maxsize=_SLICE_CACHE_SIZE, ttl=_SLICE_CACHE_TTL)
# Backend is selectable via CACHE_BACKEND (memory/redis/none, see backend_factory) so
# ECS instances can share L2 through Redis instead of each holding a private copy.
_slice_memo: CacheBackend = create_memoizer(
    namespace="l2", memory_cache=_slice_cache, ttl_seconds=_SLICE_CACHE_TTL
)


def _compute_slice_from_store(
    store_url: str, date: str, variables: list[str], ocean_masked: bool = False
) -> xr.Dataset:
    """Fetch a 2-D slice from the Zarr store. Both `load_slice` and
    `load_slice_uncached` delegate here; they differ only in whether the
    result lands in L2.

    When ``ocean_masked`` is set, anomalous values outside the model's valid ocean
    domain are nulled here (masks.apply_ocean_mask) so every downstream consumer
    inherits the cut.
    """
    result = _fetch_slice_from_store(store_url, date, variables)
    if ocean_masked:
        result = apply_ocean_mask(result, variables)
    return result


def _fetch_slice_from_store(store_url: str, date: str, variables: list[str]) -> xr.Dataset:
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
        logger.debug(
            "[timing] slice loaded from S3",
            extra={"date": date, "s3_fetch_ms": round(elapsed * 1000, 1)},
        )
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
def load_slice(
    store_url: str, date: str, variables: list[str], ocean_masked: bool = False
) -> xr.Dataset:
    """
    Return a fully-computed 2D (lat × lon) slice for the given store, date, and variables.
    Uses nearest-match on time so callers don't need to ask exact timestamps.
    Coordinate names are already normalised by the store registry.

    ``ocean_masked`` (from ``Product.ocean_masked``) nulls anomalous values outside
    the valid model domain. It's a deterministic function of the cache key (a store
    + variable set maps to one product), so it stays out of the key; the masked
    slice is what L2 caches.
    """
    cache_key = (store_url, date, tuple(sorted(variables)))
    return _slice_memo.get_or_compute(
        cache_key, lambda: _compute_slice_from_store(store_url, date, variables, ocean_masked)
    )


def load_slice_uncached(
    store_url: str, date: str, variables: list[str], ocean_masked: bool = False
) -> xr.Dataset:
    """Return a 2-D slice without touching the L2 in-memory cache.

    Pulls directly from the Zarr store. Used by the animation endpoint so a
    rare multi-date request doesn't evict another product's hot slices from
    the shared L2 LRU.
    """
    return _compute_slice_from_store(store_url, date, variables, ocean_masked)
