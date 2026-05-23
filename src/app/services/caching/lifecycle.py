"""Cross-layer cache lifecycle: prewarm, refresh, and product-eviction fan-out.

This module owns the orchestration that touches every cache layer (L1 processed
grids, L2 slices, L3 disk) and the store registry. Those layers do **not**
depend on each other — that one-way arrow is the reason this module exists.
Previously ``loader.evict_product_cache`` and ``disk_cache._prewarm_one``
reached across layers via function-local imports to dodge load-time cycles;
centralising the orchestration here removes those local imports.

Entry points:
  * ``prewarm_disk_slices`` — populate L3 from S3 on startup (parallel workers).
  * ``refresh_disk_cache`` — periodic top-up: add newly available dates, evict
    dates outside each product's window, drop orphan product dirs.
  * ``evict_stale_and_orphans`` — refresh's eviction half, called standalone on
    startup before prewarm so the cache reflects the current product/date state
    from the moment the server starts serving.
  * ``evict_product_cache`` — fan-out eviction across L1/L2/L3 when a product
    is deregistered.

The eviction functions take the **full** registered product list — passing a
subset (e.g. a single product from an admin POST) would wipe every other
product's cache. Callers must respect this.
"""

import logging
import os
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import anyio

from app.config.paths import DISK_CACHE_PATH
from app.services.caching.disk import (
    disk_cache_path,
    evict_if_over_threshold,
    evict_product_dir,
    write_slice_to_disk,
)
from app.services.caching.processed_cache import evict_processed_cache
from app.services.caching.slice_cache import (
    evict_slice_cache_for_product,
    load_slice_uncached,
)
from app.services.product.product import Product
from app.services.product.registry import get_product
from app.services.store.registry import get_available_dates

logger = logging.getLogger(__name__)

_CACHE_DAYS = int(os.environ.get("CACHE_DAYS", 30))

# Concurrency gate for prewarm/refresh S3 fan-out. Acquired *before* spawning
# each task in the task group so at most N coroutines exist at a time — bounds
# both the active S3 connections and the pending-coroutine count (gather'd over
# product × date this can be thousands of jobs).
# Bound to the S3 connection ceiling, not CPU.
_PREWARM_SEM = anyio.Semaphore(int(os.environ.get("PREWARM_WORKERS", 8)))

# Status of the most recent refresh_disk_cache run, surfaced via /admin/cache.
# "never_run" until the first cycle starts; flips to "running" while in progress,
# then "ok" or "error". We track both start and completion so /admin/cache can
# distinguish "currently running" from "last finished at X".
_refresh_status: dict = {
    "last_started_at": None,
    "last_completed_at": None,
    "status": "never_run",
    "last_error": None,
}
_refresh_status_lock = threading.Lock()

_prewarm_running = False
_prewarm_lock = threading.Lock()


def is_prewarm_running() -> bool:
    with _prewarm_lock:
        return _prewarm_running


def is_refresh_running() -> bool:
    with _refresh_status_lock:
        return _refresh_status["status"] == "running"


def get_refresh_status() -> dict:
    """Snapshot of the most recent refresh_disk_cache run, with ISO-8601 UTC timestamps."""
    with _refresh_status_lock:
        return {
            "status": _refresh_status["status"],
            "last_started_at": (
                _refresh_status["last_started_at"].isoformat()
                if _refresh_status["last_started_at"]
                else None
            ),
            "last_completed_at": (
                _refresh_status["last_completed_at"].isoformat()
                if _refresh_status["last_completed_at"]
                else None
            ),
            "last_error": _refresh_status["last_error"],
            "interval_seconds": int(os.environ.get("CACHE_REFRESH_INTERVAL_SECONDS", 14400)),
        }


def _prewarm_one(product: Product, date: str, variables: list[str]) -> str:
    try:
        cache_path = disk_cache_path(product.source_path, date, variables)
        if cache_path.exists():
            return "skipped"
        # Uncached: prewarm processes many dates and would otherwise evict live-request
        # entries from the 10-slot L2 LRU.
        ds = load_slice_uncached(product.source_path, date, variables)
        # Guard against the admin add → delete race: if the product was deregistered
        # (or replaced with a different object under the same id) while we were
        # fetching from S3, don't recreate its cache dir — write_slice_to_disk would
        # silently undo the deletion and leak orphan files until the next eviction.
        if get_product(product.id) is not product:
            logger.debug(
                "Prewarm write skipped — product no longer registered",
                extra={"product_id": product.id, "date": date},
            )
            return "skipped"
        write_slice_to_disk(cache_path, ds)
        logger.debug("Disk prewarm written", extra={"product_id": product.id, "date": date})
        return "written"
    except FileNotFoundError:
        return "skipped"  # date in time index but nearest match is a different local day
    except Exception:
        logger.warning(
            "Disk prewarm failed",
            extra={"product_id": product.id, "date": date},
            exc_info=True,
        )
        return "failed"


async def prewarm_disk_slices(products: list[Product]) -> None:
    """Populate L2 from disk on startup; write to disk for any dates not yet cached.

    Parallelises across (product, date) pairs via the anyio thread pool. The
    spawn loop acquires ``_PREWARM_SEM`` *before* ``tg.start_soon``, so at most
    PREWARM_WORKERS (default 8) tasks exist at once — bounds both S3 fan-out and
    the pending-coroutine count when product × date is large.
    """
    global _prewarm_running
    with _prewarm_lock:
        _prewarm_running = True

    t0 = time.monotonic()
    try:
        jobs: list[tuple[Product, str, list[str]]] = []
        for product in products:
            variables = product.variables
            try:
                dates = await anyio.to_thread.run_sync(get_available_dates, product.source_path)
                dates = dates[-_CACHE_DAYS:]
            except Exception:
                logger.warning(
                    "Prewarm: could not get dates",
                    extra={"product_id": product.id},
                    exc_info=True,
                )
                continue
            jobs.extend((product, date, variables) for date in dates)

        counts: dict[str, int] = {"written": 0, "skipped": 0, "failed": 0}

        # Tasks all run on the loop's thread; counts[r] += 1 has no await
        # between read and write, so no lock is needed.
        async def _run_one(p: Product, d: str, v: list[str]) -> None:
            try:
                r = await anyio.to_thread.run_sync(_prewarm_one, p, d, v)
                # .get() defensively: a future _prewarm_one return value not seeded
                # in counts would otherwise raise KeyError and abort the task group.
                counts[r] = counts.get(r, 0) + 1
            finally:
                _PREWARM_SEM.release()

        async with anyio.create_task_group() as tg:
            for p, d, v in jobs:
                await _PREWARM_SEM.acquire()
                # If start_soon raises (task group cancellation), release the token we
                # just acquired — otherwise the semaphore leaks for the process lifetime.
                try:
                    tg.start_soon(_run_one, p, d, v)
                except BaseException:
                    _PREWARM_SEM.release()
                    raise

        logger.info(
            "Prewarm complete",
            extra={
                "written": counts["written"],
                "skipped": counts["skipped"],
                "failed": counts["failed"],
                "seconds": round(time.monotonic() - t0, 1),
            },
        )

        # Inside the try so is_prewarm_running() stays true through the trailing
        # eviction; otherwise /admin/cache/disk could race against this unlink pass.
        await anyio.to_thread.run_sync(evict_if_over_threshold)
    finally:
        with _prewarm_lock:
            _prewarm_running = False


def evict_stale_and_orphans(products: list[Product]) -> None:
    """Remove cached dates outside each product's window and cache dirs for unknown products.

    Callers MUST pass the full set of currently registered products. Anything not in
    `products` is treated as orphaned and removed — passing a single-product list (as
    admin POST does) would wipe every other product's cache.
    """
    evict_if_over_threshold()

    stale = 0
    orphans = 0
    for product in products:
        variables = product.variables
        try:
            target_dates = set(get_available_dates(product.source_path)[-_CACHE_DAYS:])
        except Exception:
            logger.warning(
                "Evict: could not get dates",
                extra={"product_id": product.id},
                exc_info=True,
            )
            continue

        cache_dir = disk_cache_path(product.source_path, "", variables).parent
        if not cache_dir.exists():
            continue
        cached_dates = {f.name.split(".")[0] for f in cache_dir.glob("*.pkl.lz4")}

        for date in sorted(cached_dates - target_dates):
            p = disk_cache_path(product.source_path, date, variables)
            if p.exists():
                p.unlink()
                stale += 1
                logger.debug(
                    "Disk cache evicted (stale)",
                    extra={"product_id": product.id, "date": date},
                )

    # Remove cache dirs for products no longer registered
    base_path = Path(DISK_CACHE_PATH)
    if base_path.exists():
        known_dirs = {disk_cache_path(p.source_path, "", p.variables).parent.name for p in products}
        for entry in base_path.iterdir():
            if entry.is_dir() and entry.name not in known_dirs:
                shutil.rmtree(entry, ignore_errors=True)
                orphans += 1
                logger.debug(
                    "Disk cache evicted (orphaned product)",
                    extra={"dir_name": entry.name},
                )

    if stale or orphans:
        logger.info(
            "Cache eviction complete",
            extra={"stale_dates": stale, "orphan_dirs": orphans},
        )


async def refresh_disk_cache(products: list[Product]) -> None:
    """Add newly available dates to disk cache; evict dates outside each product's window.

    Callers MUST pass the full set of currently registered products — see
    [[evict_stale_and_orphans]] for why.
    """
    with _refresh_status_lock:
        _refresh_status["last_started_at"] = datetime.now(UTC)
        _refresh_status["status"] = "running"

    t0 = time.monotonic()
    try:
        await anyio.to_thread.run_sync(evict_stale_and_orphans, products)

        jobs: list[tuple[Product, str, list[str]]] = []
        for product in products:
            variables = product.variables
            try:
                target_dates_list = await anyio.to_thread.run_sync(
                    get_available_dates, product.source_path
                )
                target_dates = set(target_dates_list[-_CACHE_DAYS:])
            except Exception:
                logger.warning(
                    "Refresh: could not get dates",
                    extra={"product_id": product.id},
                    exc_info=True,
                )
                continue

            cache_dir = disk_cache_path(product.source_path, "", variables).parent
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached_dates = {f.name.split(".")[0] for f in cache_dir.glob("*.pkl.lz4")}

            jobs.extend((product, date, variables) for date in sorted(target_dates - cached_dates))

        added = 0
        failed = 0

        async def _run_one(p: Product, d: str, v: list[str]) -> None:
            nonlocal added, failed
            try:
                r = await anyio.to_thread.run_sync(_prewarm_one, p, d, v)
                if r == "written":
                    added += 1
                elif r == "failed":
                    failed += 1
            finally:
                _PREWARM_SEM.release()

        async with anyio.create_task_group() as tg:
            for p, d, v in jobs:
                await _PREWARM_SEM.acquire()
                # If start_soon raises (task group cancellation), release the token we
                # just acquired — otherwise the semaphore leaks for the process lifetime.
                try:
                    tg.start_soon(_run_one, p, d, v)
                except BaseException:
                    _PREWARM_SEM.release()
                    raise

        await anyio.to_thread.run_sync(evict_if_over_threshold)
        logger.info(
            "Refresh cycle complete",
            extra={
                "added": added,
                "failed": failed,
                "seconds": round(time.monotonic() - t0, 1),
            },
        )
    except Exception as e:
        with _refresh_status_lock:
            _refresh_status["last_completed_at"] = datetime.now(UTC)
            _refresh_status["status"] = "error"
            _refresh_status["last_error"] = f"{type(e).__name__}: {e}"
        raise
    else:
        with _refresh_status_lock:
            _refresh_status["last_completed_at"] = datetime.now(UTC)
            _refresh_status["status"] = "ok"
            _refresh_status["last_error"] = None


def evict_product_cache(product: Product) -> None:
    """Remove all in-memory and disk cache entries for a deleted product."""
    removed = evict_slice_cache_for_product(product)
    if removed:
        logger.info(
            "Memory cache evicted for product",
            extra={"product_id": product.id, "slices_removed": removed},
        )

    evict_processed_cache(product)
    evict_product_dir(product)
