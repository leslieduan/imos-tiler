import concurrent.futures
import logging
import os
import pickle
import shutil
import threading
import time
from pathlib import Path

import lz4.frame
import pandas as pd
import xarray as xr
from cachetools import LRUCache

from constants import COORD_NAMES, Product
from utils.dates import LOCAL_TZ, ts_to_local_date
from utils.memoizer import Memoizer

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

# Per-store map of {local_date: [timestamps]} derived from the time coord.
# Computing ts_to_local_date for every timestamp is pure-Python and O(N); doing it
# on every load_slice / get_available_dates call costs milliseconds against stores
# with thousands of timestamps. Build once per store open, refresh in lock-step
# with the dataset itself so date lookups stay O(1) on the hot path.
_date_index: dict[str, dict[str, list]] = {}

# Separate lock for product.lod_grids lazy initialization.
_lod_grids_lock = threading.Lock()

_SLICE_CACHE_SIZE = int(os.environ.get("SLICE_CACHE_SIZE", 10))
_CACHE_DAYS = int(os.environ.get("CACHE_DAYS", 30))
_slice_cache: LRUCache = LRUCache(maxsize=_SLICE_CACHE_SIZE)
_slice_memo: Memoizer = Memoizer(_slice_cache)


def _disk_cache_path(store_url: str, date: str, variables: list[str]) -> Path | None:
    base = os.environ.get("DISK_CACHE_PATH")
    if not base:
        return None
    # Encode the full URL into a single directory name so two stores with the same
    # basename from different buckets (e.g. s3://a/sla.zarr and s3://b/sla.zarr) do
    # not collide. '%' is disallowed in S3 bucket names and effectively never used
    # in S3 object keys, so the substitution is bijective in practice.
    store_name = store_url.rstrip("/").replace("/", "%")
    var_str = ",".join(sorted(variables))
    return Path(base) / f"{store_name}-{var_str}" / f"{date}.pkl.lz4"


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


def _build_date_index(ds: xr.Dataset) -> dict[str, list]:
    """Return {local_date: [timestamps]} for the dataset's time coord, or {} if missing.

    Computed outside any lock so the hot path doesn't block on the O(N) conversion.
    """
    if "time" not in ds.dims:
        return {}
    index: dict[str, list] = {}
    for ts in ds.coords["time"].values:
        index.setdefault(ts_to_local_date(ts), []).append(ts)
    return index


def _publish_store(store_url: str, ds: xr.Dataset, index: dict[str, list]) -> None:
    """Atomically replace store, opened-at timestamp, and date index for a URL."""
    with _store_lock:
        _stores[store_url] = ds
        _store_opened_at[store_url] = time.monotonic()
        _date_index[store_url] = index


def _refresh_store_background(store_url: str) -> None:
    try:
        ds = _open_store(store_url)
        index = _build_date_index(ds)
        _publish_store(store_url, ds, index)
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
        index = _build_date_index(ds)
        _publish_store(store_url, ds, index)
        future.set_result(ds)
    except Exception as e:
        future.set_exception(e)
        raise
    finally:
        with _store_lock:
            _store_in_flight.pop(store_url, None)
    return ds


def prewarm_stores(store_urls: list[str]) -> None:
    """Moves the one-time S3 metadata cost from the first user request to server startup,
    where it's invisible. Also enables get_products_availability to respond fast on first call."""
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


# Serialises disk-pressure evictions. Without this, concurrent prewarm workers
# could each scan the cache, decide to evict the same files, and either race on
# unlink or over-evict. Holding the lock around scan+evict keeps the decision
# consistent and bounds the eviction pass to one thread at a time.
_evict_lock = threading.Lock()


def _evict_disk_if_needed() -> None:
    base = os.environ.get("DISK_CACHE_PATH")
    if not base:
        return
    limit_bytes = int(os.environ.get("DISK_CACHE_LIMIT_GB", 20)) * 1024**3
    threshold = int(limit_bytes * float(os.environ.get("DISK_EVICTION_THRESHOLD", 0.85)))

    with _evict_lock:
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


def _write_slice_to_disk(cache_path: Path, ds: xr.Dataset) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(lz4.frame.compress(pickle.dumps(ds)))


def _prewarm_one(product: Product, date: str, variables: list[str]) -> None:
    try:
        cache_path = _disk_cache_path(product.source_path, date, variables)
        if cache_path is not None and cache_path.exists():
            return
        ds = load_slice(product.source_path, date, variables)
        if cache_path is not None:
            _write_slice_to_disk(cache_path, ds)
            logger.info("Disk prewarm written (S3): %s / %s", product.id, date)
    except FileNotFoundError:
        pass  # date in time index but nearest match is a different local day — not cacheable
    except Exception:
        logger.warning("Disk prewarm failed: %s / %s", product.id, date, exc_info=True)


def prewarm_disk_slices(products: list[Product]) -> None:
    """Populate L2 from disk on startup; write to disk for any dates not yet cached.

    Parallelises across (product, date) pairs — PREWARM_WORKERS controls concurrency
    (default 4). Cold S3 reads for different keys run concurrently; _slice_in_flight
    deduplicates any accidental overlap on the same key.
    """
    if not os.environ.get("DISK_CACHE_PATH"):
        return

    jobs: list[tuple[Product, str, list[str]]] = []
    for product in products:
        variables = product.variables
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

    _evict_disk_if_needed()


def evict_stale_and_orphans(products: list[Product]) -> None:
    """Remove cached dates outside each product's window and cache dirs for unknown products.

    Callers MUST pass the full set of currently registered products. Anything not in
    `products` is treated as orphaned and removed — passing a single-product list (as
    admin POST does) would wipe every other product's cache.
    """
    if not os.environ.get("DISK_CACHE_PATH"):
        return
    _evict_disk_if_needed()

    for product in products:
        variables = product.variables
        try:
            target_dates = set(get_available_dates(product.source_path)[-_CACHE_DAYS:])
        except Exception:
            logger.warning("Evict: could not get dates for %s", product.id, exc_info=True)
            continue

        _p = _disk_cache_path(product.source_path, "", variables)
        if _p is None:
            continue
        cache_dir = _p.parent
        if not cache_dir.exists():
            continue
        cached_dates = {f.name.split(".")[0] for f in cache_dir.glob("*.pkl.lz4")}

        for date in sorted(cached_dates - target_dates):
            p = _disk_cache_path(product.source_path, date, variables)
            if p is not None and p.exists():
                p.unlink()
                logger.info("Disk cache evicted (stale): %s / %s", product.id, date)

    # Remove cache dirs for products no longer registered
    base = os.environ.get("DISK_CACHE_PATH")
    base_path = Path(base) if base else None
    if base_path and base_path.exists():
        known_dirs = set()
        for product in products:
            _p = _disk_cache_path(product.source_path, "", product.variables)
            if _p is not None:
                known_dirs.add(_p.parent.name)
        for entry in base_path.iterdir():
            if entry.is_dir() and entry.name not in known_dirs:
                shutil.rmtree(entry, ignore_errors=True)
                logger.info("Disk cache evicted (orphaned product): %s", entry.name)


def refresh_disk_cache(products: list[Product]) -> None:
    """Add newly available dates to disk cache; evict dates outside each product's window.

    Callers MUST pass the full set of currently registered products — see
    [[evict_stale_and_orphans]] for why.
    """
    if not os.environ.get("DISK_CACHE_PATH"):
        return
    evict_stale_and_orphans(products)

    for product in products:
        variables = product.variables
        try:
            target_dates = set(get_available_dates(product.source_path)[-_CACHE_DAYS:])
        except Exception:
            logger.warning("Refresh: could not get dates for %s", product.id, exc_info=True)
            continue

        _p = _disk_cache_path(product.source_path, "", variables)
        if _p is None:
            continue
        cache_dir = _p.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_dates = {f.name.split(".")[0] for f in cache_dir.glob("*.pkl.lz4")}

        for date in sorted(target_dates - cached_dates):
            try:
                ds = load_slice(product.source_path, date, variables)
                p = _disk_cache_path(product.source_path, date, variables)
                if p is not None:
                    _write_slice_to_disk(p, ds)
                    logger.info("Disk cache added: %s / %s", product.id, date)
            except Exception:
                logger.warning("Disk cache add failed: %s / %s", product.id, date, exc_info=True)

    _evict_disk_if_needed()


def evict_product_cache(product: "Product") -> None:
    """Remove all in-memory and disk cache entries for a deleted product."""
    from services.data_renderer import evict_processed_cache

    variables = product.variables
    vars_tuple = tuple(sorted(variables))

    removed = _slice_memo.evict_matching(
        lambda k: k[0] == product.source_path and k[2] == vars_tuple
    )
    if removed:
        logger.info("Memory cache evicted %d slice(s) for: %s", removed, product.id)

    evict_processed_cache(product)

    _p = _disk_cache_path(product.source_path, "", variables)
    if _p is not None and _p.parent.exists():
        shutil.rmtree(_p.parent, ignore_errors=True)
        logger.info("Disk cache evicted (product removed): %s", product.id)


def get_available_dates(store_url: str) -> list[str]:
    _get_store(store_url)  # ensures _date_index[store_url] is populated
    with _store_lock:
        index = _date_index.get(store_url)
    return sorted(index) if index else []


# Concurrent identical requests share one S3 .compute() via _slice_memo: the first
# thread to miss runs the factory, the rest block on its Future. Errors propagate
# to all waiters and the in-flight entry is cleared, so a failed request never
# permanently blocks subsequent attempts for the same key.
def load_slice(store_url: str, date: str, variables: list[str]) -> xr.Dataset:
    """
    Return a fully-computed 2D (lat × lon) slice for the given store, date, and variables.
    Uses nearest-match on time so callers don't need to ask exact timestamps.
    Coordinate names are already normalised by _get_store.
    """
    cache_key = (store_url, date, tuple(sorted(variables)))

    def factory() -> xr.Dataset:
        cache_path = _disk_cache_path(store_url, date, list(variables))
        if cache_path is not None and cache_path.exists():
            try:
                return pickle.loads(lz4.frame.decompress(cache_path.read_bytes()))
            except Exception:
                logger.warning("Disk cache read failed: %s", cache_path, exc_info=True)

        store = _get_store(store_url)
        with _store_lock:
            matching = list(_date_index.get(store_url, {}).get(date, ()))
        if not matching:
            raise FileNotFoundError(
                f"No data for date {date!r}. Dates must be {LOCAL_TZ.key} local dates "
                f"(as returned by the manifest endpoint), not UTC."
            )
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
