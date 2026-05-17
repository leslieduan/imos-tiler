"""Disk-backed slice cache (L3).

Slices that have been ``compute()``-d from Zarr are pickled + LZ4-compressed and
written under ``DISK_CACHE_PATH``. Each (store, variable-set) gets its own
directory; each date is a single file. Disabled entirely when the env var is
unset.

Three lifecycle entry points:
  * ``prewarm_disk_slices`` — populate from S3 on startup (parallel workers).
  * ``refresh_disk_cache`` — periodic top-up: add newly available dates, evict
    dates outside each product's window, drop orphan product dirs.
  * ``evict_stale_and_orphans`` — refresh's eviction half, called standalone on
    startup before prewarm so the cache reflects the current product/date state
    from the moment the server starts serving.

The eviction functions take the **full** registered product list — passing a
subset (e.g. a single product from an admin POST) would wipe every other
product's cache. Callers must respect this.
"""

import concurrent.futures
import logging
import os
import pickle
import shutil
import threading
from pathlib import Path

import lz4.frame
import xarray as xr

from constants import Product

logger = logging.getLogger(__name__)

_CACHE_DAYS = int(os.environ.get("CACHE_DAYS", 30))


def disk_cache_path(store_url: str, date: str, variables: list[str]) -> Path | None:
    """Return the L3 cache path for a slice, or None if disk caching is disabled."""
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


def read_slice_from_disk(cache_path: Path) -> xr.Dataset | None:
    """Return the cached dataset at ``cache_path``, or None if read fails."""
    try:
        return pickle.loads(lz4.frame.decompress(cache_path.read_bytes()))
    except Exception:
        logger.warning("Disk cache read failed: %s", cache_path, exc_info=True)
        return None


def write_slice_to_disk(cache_path: Path, ds: xr.Dataset) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(lz4.frame.compress(pickle.dumps(ds)))


# Serialises disk-pressure evictions. Without this, concurrent prewarm workers
# could each scan the cache, decide to evict the same files, and either race on
# unlink or over-evict. Holding the lock around scan+evict keeps the decision
# consistent and bounds the eviction pass to one thread at a time.
_evict_lock = threading.Lock()


def _evict_if_over_threshold() -> None:
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


def _prewarm_one(product: Product, date: str, variables: list[str]) -> None:
    # Local import: load_slice lives in services.loader, which depends on this
    # module — keep the cycle out of import time.
    from services.loader import load_slice

    try:
        cache_path = disk_cache_path(product.source_path, date, variables)
        if cache_path is not None and cache_path.exists():
            return
        ds = load_slice(product.source_path, date, variables)
        if cache_path is not None:
            write_slice_to_disk(cache_path, ds)
            logger.info("Disk prewarm written (S3): %s / %s", product.id, date)
    except FileNotFoundError:
        pass  # date in time index but nearest match is a different local day — not cacheable
    except Exception:
        logger.warning("Disk prewarm failed: %s / %s", product.id, date, exc_info=True)


def prewarm_disk_slices(products: list[Product]) -> None:
    """Populate L2 from disk on startup; write to disk for any dates not yet cached.

    Parallelises across (product, date) pairs — PREWARM_WORKERS controls concurrency
    (default 8). Cold S3 reads for different keys run concurrently; the slice
    Memoizer deduplicates any accidental overlap on the same key.
    """
    if not os.environ.get("DISK_CACHE_PATH"):
        return

    from services.loader import get_available_dates

    jobs: list[tuple[Product, str, list[str]]] = []
    for product in products:
        variables = product.variables
        try:
            dates = get_available_dates(product.source_path)[-_CACHE_DAYS:]
        except Exception:
            logger.warning("Prewarm: could not get dates for %s", product.id, exc_info=True)
            continue
        jobs.extend((product, date, variables) for date in dates)

    max_workers = int(os.environ.get("PREWARM_WORKERS", 8))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="prewarm"
    ) as pool:
        for p, d, v in jobs:
            pool.submit(_prewarm_one, p, d, v)
        # pool.__exit__ calls shutdown(wait=True) — all jobs complete before returning

    _evict_if_over_threshold()


def evict_stale_and_orphans(products: list[Product]) -> None:
    """Remove cached dates outside each product's window and cache dirs for unknown products.

    Callers MUST pass the full set of currently registered products. Anything not in
    `products` is treated as orphaned and removed — passing a single-product list (as
    admin POST does) would wipe every other product's cache.
    """
    if not os.environ.get("DISK_CACHE_PATH"):
        return
    _evict_if_over_threshold()

    from services.loader import get_available_dates

    for product in products:
        variables = product.variables
        try:
            target_dates = set(get_available_dates(product.source_path)[-_CACHE_DAYS:])
        except Exception:
            logger.warning("Evict: could not get dates for %s", product.id, exc_info=True)
            continue

        _p = disk_cache_path(product.source_path, "", variables)
        if _p is None:
            continue
        cache_dir = _p.parent
        if not cache_dir.exists():
            continue
        cached_dates = {f.name.split(".")[0] for f in cache_dir.glob("*.pkl.lz4")}

        for date in sorted(cached_dates - target_dates):
            p = disk_cache_path(product.source_path, date, variables)
            if p is not None and p.exists():
                p.unlink()
                logger.info("Disk cache evicted (stale): %s / %s", product.id, date)

    # Remove cache dirs for products no longer registered
    base = os.environ.get("DISK_CACHE_PATH")
    base_path = Path(base) if base else None
    if base_path and base_path.exists():
        known_dirs = set()
        for product in products:
            _p = disk_cache_path(product.source_path, "", product.variables)
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

    from services.loader import get_available_dates, load_slice

    for product in products:
        variables = product.variables
        try:
            target_dates = set(get_available_dates(product.source_path)[-_CACHE_DAYS:])
        except Exception:
            logger.warning("Refresh: could not get dates for %s", product.id, exc_info=True)
            continue

        _p = disk_cache_path(product.source_path, "", variables)
        if _p is None:
            continue
        cache_dir = _p.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_dates = {f.name.split(".")[0] for f in cache_dir.glob("*.pkl.lz4")}

        for date in sorted(target_dates - cached_dates):
            try:
                ds = load_slice(product.source_path, date, variables)
                p = disk_cache_path(product.source_path, date, variables)
                if p is not None:
                    write_slice_to_disk(p, ds)
                    logger.info("Disk cache added: %s / %s", product.id, date)
            except Exception:
                logger.warning("Disk cache add failed: %s / %s", product.id, date, exc_info=True)

    _evict_if_over_threshold()


def evict_product_dir(product: Product) -> None:
    """Remove the on-disk cache directory for a deleted product."""
    _p = disk_cache_path(product.source_path, "", product.variables)
    if _p is not None and _p.parent.exists():
        shutil.rmtree(_p.parent, ignore_errors=True)
        logger.info("Disk cache evicted (product removed): %s", product.id)
