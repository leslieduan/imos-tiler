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
import time
from datetime import UTC, datetime
from pathlib import Path

import lz4.frame
import xarray as xr

from app.constants import DISK_CACHE_PATH, Product

logger = logging.getLogger(__name__)

_CACHE_DAYS = int(os.environ.get("CACHE_DAYS", 30))

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


def disk_cache_path(store_url: str, date: str, variables: list[str]) -> Path | None:
    """Return the L3 cache path for a slice, or None if disk caching is disabled."""
    base = DISK_CACHE_PATH
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
        logger.warning("Disk cache read failed", extra={"path": str(cache_path)}, exc_info=True)
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
    base = DISK_CACHE_PATH
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

        evicted_count = 0
        evicted_bytes = 0
        for f in entries:
            if total <= threshold:
                break
            size = f.stat().st_size
            f.unlink(missing_ok=True)
            total -= size
            evicted_count += 1
            evicted_bytes += size
            logger.debug(
                "Disk pressure: file evicted",
                extra={"path": str(f), "size_kb": size // 1024},
            )
        if evicted_count:
            usage_pct = total / limit_bytes * 100 if limit_bytes > 0 else 0.0
            logger.info(
                "Disk pressure eviction completed",
                extra={
                    "files_removed": evicted_count,
                    "mb_freed": round(evicted_bytes / 1024**2, 1),
                    "usage_pct": round(usage_pct, 1),
                },
            )


def _prewarm_one(product: Product, date: str, variables: list[str]) -> str:
    # Local import: load_slice lives in services.loader, which depends on this
    # module — keep the cycle out of import time.
    from app.services.loader import load_slice

    try:
        cache_path = disk_cache_path(product.source_path, date, variables)
        if cache_path is not None and cache_path.exists():
            return "skipped"
        ds = load_slice(product.source_path, date, variables)
        if cache_path is not None:
            write_slice_to_disk(cache_path, ds)
            logger.debug("Disk prewarm written", extra={"product_id": product.id, "date": date})
            return "written"
        return "skipped"
    except FileNotFoundError:
        return "skipped"  # date in time index but nearest match is a different local day
    except Exception:
        logger.warning(
            "Disk prewarm failed",
            extra={"product_id": product.id, "date": date},
            exc_info=True,
        )
        return "failed"


def prewarm_disk_slices(products: list[Product]) -> None:
    """Populate L2 from disk on startup; write to disk for any dates not yet cached.

    Parallelises across (product, date) pairs — PREWARM_WORKERS controls concurrency
    (default 8). Cold S3 reads for different keys run concurrently; the slice
    Memoizer deduplicates any accidental overlap on the same key.
    """
    if not DISK_CACHE_PATH:
        return

    from app.services.loader import get_available_dates

    global _prewarm_running
    with _prewarm_lock:
        _prewarm_running = True

    t0 = time.monotonic()
    try:
        jobs: list[tuple[Product, str, list[str]]] = []
        for product in products:
            variables = product.variables
            try:
                dates = get_available_dates(product.source_path)[-_CACHE_DAYS:]
            except Exception:
                logger.warning(
                    "Prewarm: could not get dates",
                    extra={"product_id": product.id},
                    exc_info=True,
                )
                continue
            jobs.extend((product, date, variables) for date in dates)

        max_workers = int(os.environ.get("PREWARM_WORKERS", 8))
        futs: list[concurrent.futures.Future[str]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="prewarm"
        ) as pool:
            for p, d, v in jobs:
                futs.append(pool.submit(_prewarm_one, p, d, v))
            # pool.__exit__ calls shutdown(wait=True) — all jobs complete before returning

        counts: dict[str, int] = {"written": 0, "skipped": 0, "failed": 0}
        for f in futs:
            counts[f.result()] += 1
        logger.info(
            "Prewarm complete",
            extra={
                "written": counts["written"],
                "skipped": counts["skipped"],
                "failed": counts["failed"],
                "seconds": round(time.monotonic() - t0, 1),
            },
        )
    finally:
        with _prewarm_lock:
            _prewarm_running = False

    _evict_if_over_threshold()


def evict_stale_and_orphans(products: list[Product]) -> None:
    """Remove cached dates outside each product's window and cache dirs for unknown products.

    Callers MUST pass the full set of currently registered products. Anything not in
    `products` is treated as orphaned and removed — passing a single-product list (as
    admin POST does) would wipe every other product's cache.
    """
    if not DISK_CACHE_PATH:
        return
    _evict_if_over_threshold()

    from app.services.loader import get_available_dates

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
                stale += 1
                logger.debug(
                    "Disk cache evicted (stale)",
                    extra={"product_id": product.id, "date": date},
                )

    # Remove cache dirs for products no longer registered
    base = DISK_CACHE_PATH
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


def refresh_disk_cache(products: list[Product]) -> None:
    """Add newly available dates to disk cache; evict dates outside each product's window.

    Callers MUST pass the full set of currently registered products — see
    [[evict_stale_and_orphans]] for why.
    """
    if not DISK_CACHE_PATH:
        return

    with _refresh_status_lock:
        _refresh_status["last_started_at"] = datetime.now(UTC)
        _refresh_status["status"] = "running"

    t0 = time.monotonic()
    try:
        evict_stale_and_orphans(products)

        from app.services.loader import get_available_dates, load_slice

        added = 0
        failed = 0
        for product in products:
            variables = product.variables
            try:
                target_dates = set(get_available_dates(product.source_path)[-_CACHE_DAYS:])
            except Exception:
                logger.warning(
                    "Refresh: could not get dates",
                    extra={"product_id": product.id},
                    exc_info=True,
                )
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
                        added += 1
                        logger.debug(
                            "Disk cache added",
                            extra={"product_id": product.id, "date": date},
                        )
                except Exception:
                    failed += 1
                    logger.warning(
                        "Disk cache add failed",
                        extra={"product_id": product.id, "date": date},
                        exc_info=True,
                    )

        _evict_if_over_threshold()
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


_EMPTY_PRODUCT_DISK_STATS: dict = {
    "file_count": 0,
    "total_bytes": 0,
    "oldest_date": None,
    "newest_date": None,
    "last_write_at": None,
}


def collect_disk_stats(products: list[Product]) -> dict:
    """Single-pass L3 cache walk producing both global and per-product stats.

    Walks ``DISK_CACHE_PATH`` once and stats every file once, then attributes
    each file to a product by matching its parent dir name against the dir name
    that ``disk_cache_path`` would produce for that product. The previous
    implementation walked the base tree for the global total and then re-walked
    each product subdir, stat'ing every file twice.

    Returns ``{"global": {...}, "per_product": {pid: {...}}}``. When disk
    caching is disabled, ``global`` is ``{"enabled": False}`` and every product
    gets the zero stats.
    """
    base = DISK_CACHE_PATH
    if not base:
        return {
            "global": {"enabled": False},
            "per_product": {p.id: dict(_EMPTY_PRODUCT_DISK_STATS) for p in products},
        }

    limit_bytes = int(os.environ.get("DISK_CACHE_LIMIT_GB", 20)) * 1024**3
    threshold_bytes = int(limit_bytes * float(os.environ.get("DISK_EVICTION_THRESHOLD", 0.85)))

    # Map "cache dir name" -> product id so we can attribute each file as we
    # encounter it. Dir name is derived the same way write paths are built, so
    # the match is exact.
    dir_to_pid: dict[str, str] = {}
    for p in products:
        _path = disk_cache_path(p.source_path, "", p.variables)
        if _path is not None:
            dir_to_pid[_path.parent.name] = p.id

    per_pid_sizes: dict[str, list[int]] = {pid: [] for pid in dir_to_pid.values()}
    per_pid_mtimes: dict[str, list[float]] = {pid: [] for pid in dir_to_pid.values()}
    per_pid_dates: dict[str, list[str]] = {pid: [] for pid in dir_to_pid.values()}
    total_bytes = 0

    base_path = Path(base)
    if base_path.exists():
        for f in base_path.rglob("*.pkl.lz4"):
            st = f.stat()
            total_bytes += st.st_size
            pid = dir_to_pid.get(f.parent.name)
            if pid is None:
                continue  # orphaned dir; counted in global, not attributed
            per_pid_sizes[pid].append(st.st_size)
            per_pid_mtimes[pid].append(st.st_mtime)
            per_pid_dates[pid].append(f.name.split(".")[0])

    per_product: dict[str, dict] = {}
    for p in products:
        sizes = per_pid_sizes.get(p.id, [])
        if not sizes:
            per_product[p.id] = dict(_EMPTY_PRODUCT_DISK_STATS)
            continue
        dates = sorted(per_pid_dates[p.id])
        per_product[p.id] = {
            "file_count": len(sizes),
            "total_bytes": sum(sizes),
            "oldest_date": dates[0],
            "newest_date": dates[-1],
            "last_write_at": datetime.fromtimestamp(max(per_pid_mtimes[p.id]), tz=UTC).isoformat(),
        }

    return {
        "global": {
            "enabled": True,
            "base_path": base,
            "total_bytes": total_bytes,
            "limit_bytes": limit_bytes,
            "eviction_threshold_bytes": threshold_bytes,
            "utilization_pct": (
                round(total_bytes / limit_bytes * 100, 2) if limit_bytes > 0 else 0.0
            ),
            "over_eviction_threshold": total_bytes > threshold_bytes,
        },
        "per_product": per_product,
    }


def evict_product_dir(product: Product) -> None:
    """Remove the on-disk cache directory for a deleted product."""
    _p = disk_cache_path(product.source_path, "", product.variables)
    if _p is not None and _p.parent.exists():
        shutil.rmtree(_p.parent, ignore_errors=True)
        logger.info("Disk cache evicted (product removed)", extra={"product_id": product.id})


def clear_disk_cache() -> dict:
    """Remove every per-product directory from the L3 disk cache.

    Returns ``{"files": N, "directories": M}`` — or zeros when disk caching is disabled.
    """
    base = DISK_CACHE_PATH
    if not base:
        return {"files": 0, "directories": 0}
    base_path = Path(base)
    if not base_path.exists():
        return {"files": 0, "directories": 0}
    files, dirs = 0, 0
    for entry in base_path.iterdir():
        if entry.is_dir():
            files += sum(1 for _ in entry.rglob("*.pkl.lz4"))
            shutil.rmtree(entry, ignore_errors=True)
            dirs += 1
    logger.info("Disk cache cleared", extra={"files": files, "directories": dirs})
    return {"files": files, "directories": dirs}
