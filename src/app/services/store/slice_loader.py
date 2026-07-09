"""Slice loading.

``load_slice`` returns a fully-computed 2-D slice for a (store, date,
variables) tuple. Concurrent identical requests always share one compute
in-process via ``_slice_dedup`` (independent of ``CACHE_BACKEND``); when
``CACHE_BACKEND=redis``, ``slice_memo`` additionally coalesces across
instances and caches the result (see ``services.caching.slice_cache``).

Long-lived store handles live in their own module ([[store.registry]]).
"""

import pandas as pd
import xarray as xr

from app.services.caching.deduper import Deduper
from app.services.caching.slice_cache import slice_memo
from app.services.rendering.masks import apply_ocean_mask
from app.services.store.registry import get_store, store_registry

# Always in-process, independent of CACHE_BACKEND — see Deduper's docstring
# for why this matters even (especially) under CACHE_BACKEND=none.
_slice_dedup = Deduper()


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
        return store[variables].sel(time=pd.Timestamp(matching[0])).compute()
    except KeyError as e:
        raise FileNotFoundError(f"No data found for date {date}") from e


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

    def compute() -> xr.Dataset:
        return slice_memo.get_or_compute(
            cache_key, lambda: _compute_slice_from_store(store_url, date, variables, ocean_masked)
        )

    return _slice_dedup.dedupe(cache_key, compute)


def load_slice_uncached(
    store_url: str, date: str, variables: list[str], ocean_masked: bool = False
) -> xr.Dataset:
    """Return a 2-D slice without touching L2.

    Pulls directly from the Zarr store. Used by the animation endpoint so a
    rare multi-date request doesn't evict another product's hot slices from
    the shared L2 cache (CACHE_BACKEND=redis).
    """
    return _compute_slice_from_store(store_url, date, variables, ocean_masked)
