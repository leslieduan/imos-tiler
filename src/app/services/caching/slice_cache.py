"""In-memory slice loading (L2).

Two responsibilities:
  * ``load_slice`` — return a fully-computed 2-D slice for a (store, date,
    variables) tuple. Cached in an LRU (L2); checks the L3 disk cache before
    falling through to S3. Concurrent identical requests share one compute via
    the slice Memoizer.
  * ``evict_slice_cache_for_product`` — narrow L2-only eviction helper used by
    [[caching.lifecycle.evict_product_cache]] (the cross-layer fan-out).

Long-lived store handles, disk IO, and cross-layer lifecycle live in their own
modules ([[store.registry]], [[caching.disk]], [[caching.lifecycle]]).
"""

import logging
import os
import time

import pandas as pd
import xarray as xr
from cachetools import TTLCache

from app.services.caching.disk import disk_cache_path, read_slice_from_disk
from app.services.product.product import Product
from app.services.store.registry import get_store, store_registry
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


def _compute_slice_from_store(store_url: str, date: str, variables: list[str]) -> xr.Dataset:
    """Fetch a 2-D slice from L3 disk cache or fall through to the Zarr store.

    Read-only with respect to L3 — populating the disk cache is the prewarmer's
    job, not the request path's. Both `load_slice` and `load_slice_uncached`
    delegate here; they differ only in whether the result lands in L2.
    """
    cache_path = disk_cache_path(store_url, date, list(variables))
    if cache_path.exists():
        t0 = time.monotonic()
        cached = read_slice_from_disk(cache_path)
        disk_ms = (time.monotonic() - t0) * 1000
        if cached is not None:
            logger.debug(
                "[timing] slice loaded from disk",
                extra={"date": date, "disk_read_ms": round(disk_ms, 1)},
            )
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


def load_point_series(
    store_url: str,
    variables: list[str],
    lat: float,
    lon: float,
    from_date: str,
    to_date: str | None,
) -> tuple[float, float, list[str], xr.Dataset | None]:
    """Return a point time series: ``(actual_lat, actual_lon, dates, point_ds)``.

    Selects the grid cell nearest to ``(lat, lon)``, then every timestamp whose
    *local* date falls in ``[from_date, to_date]`` (inclusive; ``to_date=None``
    means unbounded), and computes a single 1-D-over-time slice in one shot.

    Dates are resolved through the same store date index as [[load_slice]], so the
    local-time API invariant holds: ``dates`` are the index's local-date keys, never
    recomputed from the timestamps here. Selecting the point *before* compute keeps
    the S3 read to the spatial chunk(s) covering the cell rather than full grids.

    Bypasses the L2/L3 caches entirely — a series can span dates outside the
    prewarmed window, and per-point reads shouldn't evict another product's hot
    slices (same rationale as [[load_slice_uncached]]).

    Returns ``dates == []`` and ``point_ds is None`` when no timestamp falls in
    range (including a store with no time dimension); the nearest cell is still
    resolved so the caller can report which point it snapped to.
    """
    store = get_store(store_url)
    index = store_registry.date_index(store_url)
    dates = sorted(d for d in index if d >= from_date and (to_date is None or d <= to_date))
    point = store[variables].sel(lat=lat, lon=lon, method="nearest")
    if not dates:
        return float(point.lat), float(point.lon), [], None

    timestamps = [index[d][0] for d in dates]
    t0 = time.monotonic()
    point = point.sel(time=timestamps).compute()
    elapsed = time.monotonic() - t0
    logger.debug(
        "[timing] point series loaded from S3",
        extra={
            "store_url": store_url,
            "points": len(dates),
            "s3_fetch_ms": round(elapsed * 1000, 1),
        },
    )
    if elapsed > _SLOW_FETCH_THRESHOLD:
        logger.warning(
            "Slow S3 fetch",
            extra={"store_url": store_url, "points": len(dates), "seconds": elapsed},
        )
    return float(point.lat), float(point.lon), dates, point


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


def evict_slice_cache_for_product(product: Product) -> int:
    """Evict L2 slice cache entries belonging to ``product``. Returns count removed.

    Narrow helper exposed for [[caching.lifecycle.evict_product_cache]] so the
    cross-layer fan-out can drop L2 entries without reaching into ``_slice_memo``
    internals.
    """
    vars_tuple = tuple(sorted(product.variables))
    return _slice_memo.evict_matching(lambda k: k[0] == product.source_path and k[2] == vars_tuple)
