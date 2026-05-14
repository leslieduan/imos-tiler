import concurrent.futures
import logging
import os
import pickle
import shutil
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import lz4.frame
import pandas as pd
import xarray as xr
from cachetools import LRUCache

from constants import COORD_NAMES, Product

_LOCAL_TZ = ZoneInfo("Australia/Sydney")

logger = logging.getLogger(__name__)

# Dataset stores opened once per URL and reused across requests.
# After STORE_TTL_SECONDS the stale store is served immediately while a background thread
# re-opens it — requests never block waiting for a refresh, only for the initial open.
_STORE_TTL = float(os.environ.get("STORE_TTL_SECONDS", 600))
_stores: dict[str, xr.Dataset] = {}
_store_opened_at: dict[str, float] = {}
_store_refreshing: set[str] = set()  # URLs with a background re-open in progress
_store_lock = threading.Lock()
_store_in_flight: dict[str, concurrent.futures.Future] = {}

# Separate lock for product.lod_grids lazy initialization.
_lod_grids_lock = threading.Lock()

# Cache of fully-computed 2D (lat × lon) slices keyed by (store_url, date, variables).
# Each slice is ~7 MB (4 variables × 351 × 641 × float64).
# SLICE_CACHE_SIZE controls how many slices to hold in memory (default 50 ≈ 350 MB).
# Raise this as you add products/dates; lower it on memory-constrained deployments.
_SLICE_CACHE_SIZE = int(os.environ.get("SLICE_CACHE_SIZE", 100))
_CACHE_DAYS = int(os.environ.get("CACHE_DAYS", 30))
_slice_cache: LRUCache = LRUCache(maxsize=_SLICE_CACHE_SIZE)
_slice_lock = threading.Lock()
_slice_in_flight: dict[tuple, concurrent.futures.Future] = {}


def _disk_cache_dir(store_url: str, variables: list[str]) -> Path | None:
    base = os.environ.get("DISK_CACHE_PATH")
    if not base:
        return None
    store_name = store_url.rstrip("/").split("/")[-1]
    var_str = ",".join(sorted(variables))
    return Path(base) / f"{store_name}-{var_str}"


def _disk_cache_path(store_url: str, date: str, variables: list[str]) -> Path | None:
    d = _disk_cache_dir(store_url, variables)
    return d / f"{date}.pkl.lz4" if d is not None else None


def _open_store(store_url: str) -> xr.Dataset:
    ds = xr.open_zarr(store_url, storage_options={"anon": True})
    rename = {k: v for k, v in COORD_NAMES.items() if k in ds.dims or k in ds.coords}
    if rename:
        ds = ds.rename(rename)
    if "lat" not in ds.dims or "lon" not in ds.dims:
        raise ValueError(
            f"Store {store_url!r} missing lat/lon dims after rename (found: {list(ds.dims)})"
        )
    if "time" in ds.dims:
        ds = ds.sortby("time")
    return ds


def _refresh_store_background(store_url: str) -> None:
    try:
        ds = _open_store(store_url)
        with _store_lock:
            _stores[store_url] = ds
            _store_opened_at[store_url] = time.monotonic()
        logger.info("Store refreshed: %s", store_url)
    except Exception:
        logger.exception("Background refresh failed for %s", store_url)
    finally:
        with _store_lock:
            _store_refreshing.discard(store_url)


# Fix: serialised store opens under _store_lock meant two requests for *different* store URLs
# arriving before either had finished opening would block each other unnecessarily — the second
# request had to wait for the first store's full xr.open_zarr() even though they were unrelated.
# Resolution: replace the single global lock with a per-URL Future in _store_in_flight. The first
# thread to request a URL creates the Future and does the open; any other thread requesting the
# same URL concurrently waits on that same Future instead of blocking all other URLs too.
def _get_store(store_url: str) -> xr.Dataset:
    should_open = False
    with _store_lock:
        if store_url in _stores:
            if time.monotonic() - _store_opened_at[store_url] < _STORE_TTL:
                return _stores[store_url]
            # TTL expired — return stale store and trigger a background refresh.
            if store_url not in _store_refreshing:
                _store_refreshing.add(store_url)
                threading.Thread(
                    target=_refresh_store_background, args=(store_url,), daemon=True
                ).start()
            return _stores[store_url]
        if store_url in _store_in_flight:
            future = _store_in_flight[store_url]
        else:
            future: concurrent.futures.Future = concurrent.futures.Future()
            _store_in_flight[store_url] = future
            should_open = True

    if not should_open:
        return future.result()

    # First-ever open: block until complete.
    try:
        ds = _open_store(store_url)
        with _store_lock:
            _stores[store_url] = ds
            _store_opened_at[store_url] = time.monotonic()
        future.set_result(ds)
    except Exception as e:
        future.set_exception(e)
        raise
    finally:
        with _store_lock:
            _store_in_flight.pop(store_url, None)
    return ds


def prewarm_stores(store_urls: list[str]) -> None:
    for url in store_urls:
        threading.Thread(target=_get_store, args=(url,), daemon=True).start()


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


def _evict_disk_if_needed() -> None:
    base = os.environ.get("DISK_CACHE_PATH")
    if not base:
        return
    limit_bytes = int(os.environ.get("DISK_CACHE_LIMIT_GB", 20)) * 1024**3
    threshold = int(limit_bytes * float(os.environ.get("DISK_EVICTION_THRESHOLD", 0.85)))

    all_files = list(Path(base).rglob("*.pkl.lz4"))
    if not all_files:
        return
    total = sum(f.stat().st_size for f in all_files)
    if total <= threshold:
        return

    # Sort: smallest file first, oldest date first within same size.
    # Small-grid products are cheap to re-fetch from S3; evict them before large ones.
    entries = sorted(all_files, key=lambda f: (f.stat().st_size, f.name.split(".")[0]))

    for f in entries:
        if total <= threshold:
            break
        size = f.stat().st_size
        f.unlink(missing_ok=True)
        total -= size
        logger.info("Disk evicted (pressure): %s (%d KB)", f, size // 1024)


def _prewarm_one(product: Product, date: str, variables: list[str]) -> None:
    try:
        cache_path = _disk_cache_path(product.source_path, date, variables)
        if cache_path is not None and cache_path.exists():
            return
        ds = load_slice(product.source_path, date, variables)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(lz4.frame.compress(pickle.dumps(ds)))
            logger.info("Disk prewarm written (S3): %s / %s", product.id, date)
    except Exception:
        logger.warning("Disk prewarm failed: %s / %s", product.id, date, exc_info=True)


def prewarm_disk_slices(products: list[Product]) -> None:
    """Populate L1 from disk on startup; write to disk for any dates not yet cached.

    Parallelises across (product, date) pairs — PREWARM_WORKERS controls concurrency
    (default 4). Cold S3 reads for different keys run concurrently; _slice_in_flight
    deduplicates any accidental overlap on the same key.
    """
    if not os.environ.get("DISK_CACHE_PATH"):
        return

    jobs: list[tuple[Product, str, list[str]]] = []
    for product in products:
        variables = product.variable if isinstance(product.variable, list) else [product.variable]
        try:
            dates = get_available_dates(product.source_path)[-_CACHE_DAYS:]
        except Exception:
            logger.warning("Prewarm: could not get dates for %s", product.id, exc_info=True)
            continue
        jobs.extend((product, date, variables) for date in dates)

    max_workers = int(os.environ.get("PREWARM_WORKERS", 4))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="prewarm"
    ) as pool:
        for p, d, v in jobs:
            pool.submit(_prewarm_one, p, d, v)
        # pool.__exit__ calls shutdown(wait=True) — all jobs complete before returning


def refresh_disk_cache(products: list[Product]) -> None:
    """Add newly available dates to disk cache; evict dates outside each product's window."""
    if not os.environ.get("DISK_CACHE_PATH"):
        return
    _evict_disk_if_needed()
    for product in products:
        variables = product.variable if isinstance(product.variable, list) else [product.variable]
        try:
            target_dates = set(get_available_dates(product.source_path)[-_CACHE_DAYS:])
        except Exception:
            logger.warning("Refresh: could not get dates for %s", product.id, exc_info=True)
            continue

        cache_dir = _disk_cache_dir(product.source_path, variables)
        if cache_dir is None:
            continue
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_dates = {f.name.split(".")[0] for f in cache_dir.glob("*.pkl.lz4")}

        for date in sorted(target_dates - cached_dates):
            try:
                ds = load_slice(product.source_path, date, variables)
                p = _disk_cache_path(product.source_path, date, variables)
                if p is not None:
                    p.write_bytes(lz4.frame.compress(pickle.dumps(ds)))
                    logger.info("Disk cache added: %s / %s", product.id, date)
            except Exception:
                logger.warning("Disk cache add failed: %s / %s", product.id, date, exc_info=True)

        for date in sorted(cached_dates - target_dates):
            p = _disk_cache_path(product.source_path, date, variables)
            if p is not None and p.exists():
                p.unlink()
                logger.info("Disk cache evicted (stale): %s / %s", product.id, date)


def evict_product_cache(product: "Product") -> None:
    """Remove all in-memory and disk cache entries for a deleted product."""
    variables = product.variable if isinstance(product.variable, list) else [product.variable]
    vars_tuple = tuple(sorted(variables))

    with _slice_lock:
        keys_to_remove = [
            k for k in _slice_cache if k[0] == product.source_path and k[2] == vars_tuple
        ]
        for k in keys_to_remove:
            del _slice_cache[k]
    if keys_to_remove:
        logger.info("Memory cache evicted %d slice(s) for: %s", len(keys_to_remove), product.id)

    cache_dir = _disk_cache_dir(product.source_path, variables)
    if cache_dir is not None and cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
        logger.info("Disk cache evicted (product removed): %s", product.id)


def _ts_to_local_date(ts) -> str:
    """Convert a UTC numpy datetime64 or Timestamp to the local Australian date string."""
    return pd.Timestamp(ts).tz_localize("UTC").tz_convert(_LOCAL_TZ).strftime("%Y-%m-%d")


def get_available_dates(store_url: str) -> list[str]:
    store = _get_store(store_url)
    if "time" not in store.dims:
        return []
    return [_ts_to_local_date(ts) for ts in store.coords["time"].values]


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
        cache_path = _disk_cache_path(store_url, date, list(variables))
        if cache_path is not None and cache_path.exists():
            try:
                ds = pickle.loads(lz4.frame.decompress(cache_path.read_bytes()))
                with _slice_lock:
                    _slice_cache[cache_key] = ds
                future.set_result(ds)
                return ds
            except Exception:
                logger.warning("Disk cache read failed: %s", cache_path, exc_info=True)

        store = _get_store(store_url)
        local_midnight = pd.Timestamp(date, tz=_LOCAL_TZ).tz_convert("UTC").tz_localize(None)
        ds = store[variables].sel(time=local_midnight, method="nearest").compute()
        selected_date = _ts_to_local_date(ds.time.values)
        if selected_date != date:
            raise FileNotFoundError(
                f"No data for date {date!r} (nearest available: {selected_date!r})"
            )
        with _slice_lock:
            _slice_cache[cache_key] = ds
        future.set_result(ds)
    except KeyError as e:
        exc = FileNotFoundError(f"No data found for date {date}")
        future.set_exception(exc)
        raise exc from e
    except Exception as e:
        future.set_exception(e)
        raise
    finally:
        with _slice_lock:
            _slice_in_flight.pop(cache_key, None)
    return ds
