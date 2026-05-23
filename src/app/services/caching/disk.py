"""Disk-backed slice cache (L3) — IO + per-file eviction primitives.

Slices that have been ``compute()``-d from Zarr are pickled + LZ4-compressed and
written under ``DISK_CACHE_PATH``. Each (store, variable-set) gets its own
directory; each date is a single file.

This module exposes only the low-level operations: path derivation, read/write,
disk-pressure eviction, per-product dir eviction, top-level clear, and stats.
Cross-layer lifecycle (prewarm, refresh, stale eviction, product fan-out) lives
in [[caching.lifecycle]].
"""

import logging
import os
import pickle
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

import lz4.frame
import xarray as xr

from app.config.paths import DISK_CACHE_PATH
from app.services.product.product import Product

logger = logging.getLogger(__name__)


def disk_cache_path(store_url: str, date: str, variables: list[str]) -> Path:
    """Return the L3 cache path for a slice."""
    # Encode the full URL into a single directory name so two stores with the same
    # basename from different buckets (e.g. s3://a/sla.zarr and s3://b/sla.zarr) do
    # not collide. '%' is disallowed in S3 bucket names and effectively never used
    # in S3 object keys, so the substitution is bijective in practice.
    store_name = store_url.rstrip("/").replace("/", "%")
    var_str = ",".join(sorted(variables))
    return Path(DISK_CACHE_PATH) / f"{store_name}-{var_str}" / f"{date}.pkl.lz4"


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


def evict_if_over_threshold() -> None:
    limit_bytes = int(os.environ.get("DISK_CACHE_LIMIT_GB", 20)) * 1024**3
    threshold = int(limit_bytes * float(os.environ.get("DISK_EVICTION_THRESHOLD", 0.85)))

    with _evict_lock:
        entries = [(f, f.stat().st_size) for f in Path(DISK_CACHE_PATH).rglob("*.pkl.lz4")]
        if not entries:
            return
        total = sum(size for _, size in entries)
        if total <= threshold:
            return

        # Sort: smallest file first, oldest date first within same size.
        # Small-grid products are cheap to re-fetch from S3; evict them before large ones.
        entries.sort(key=lambda e: (e[1], e[0].name.split(".")[0]))

        evicted_count = 0
        evicted_bytes = 0
        for f, size in entries:
            if total <= threshold:
                break
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


_EMPTY_PRODUCT_DISK_STATS: dict = {
    "file_count": 0,
    "total_bytes": 0,
    "oldest_date": None,
    "newest_date": None,
    "last_write_at": None,
    "files": [],
}


def collect_disk_stats(products: list[Product]) -> dict:
    """Single-pass L3 cache walk producing both global and per-product stats.

    Walks ``DISK_CACHE_PATH`` once and stats every file once, then attributes
    each file to a product by matching its parent dir name against the dir name
    that ``disk_cache_path`` would produce for that product. The previous
    implementation walked the base tree for the global total and then re-walked
    each product subdir, stat'ing every file twice.

    Returns ``{"global": {...}, "per_product": {pid: {...}}}``.
    """
    limit_bytes = int(os.environ.get("DISK_CACHE_LIMIT_GB", 20)) * 1024**3
    threshold_bytes = int(limit_bytes * float(os.environ.get("DISK_EVICTION_THRESHOLD", 0.85)))

    # Map "cache dir name" -> product id so we can attribute each file as we
    # encounter it. Dir name is derived the same way write paths are built, so
    # the match is exact.
    dir_to_pid: dict[str, str] = {
        disk_cache_path(p.source_path, "", p.variables).parent.name: p.id for p in products
    }

    per_pid_sizes: dict[str, list[int]] = {pid: [] for pid in dir_to_pid.values()}
    per_pid_mtimes: dict[str, list[float]] = {pid: [] for pid in dir_to_pid.values()}
    per_pid_dates: dict[str, list[str]] = {pid: [] for pid in dir_to_pid.values()}
    per_pid_files: dict[str, list[str]] = {pid: [] for pid in dir_to_pid.values()}
    total_bytes = 0

    base_path = Path(DISK_CACHE_PATH)
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
            per_pid_files[pid].append(f.name)

    per_product: dict[str, dict] = {}
    for p in products:
        cache_dir = disk_cache_path(p.source_path, "", p.variables).parent.name
        sizes = per_pid_sizes.get(p.id, [])
        if not sizes:
            per_product[p.id] = {**_EMPTY_PRODUCT_DISK_STATS, "cache_dir": cache_dir}
            continue
        dates = sorted(per_pid_dates[p.id])
        per_product[p.id] = {
            "file_count": len(sizes),
            "total_bytes": sum(sizes),
            "oldest_date": dates[0],
            "newest_date": dates[-1],
            "last_write_at": datetime.fromtimestamp(max(per_pid_mtimes[p.id]), tz=UTC).isoformat(),
            "files": sorted(per_pid_files[p.id]),
            "cache_dir": cache_dir,
        }

    return {
        "global": {
            "base_path": DISK_CACHE_PATH,
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
    cache_dir = disk_cache_path(product.source_path, "", product.variables).parent
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
        logger.info("Disk cache evicted (product removed)", extra={"product_id": product.id})


def clear_disk_cache() -> dict:
    """Remove every per-product directory from the L3 disk cache.

    Returns ``{"files": N, "directories": M}``.
    """
    base_path = Path(DISK_CACHE_PATH)
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
