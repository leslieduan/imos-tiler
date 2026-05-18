"""Read-only cache state for debugging production behaviour.

Combines disk footprint (per-product + global vs eviction threshold), refresh
loop status, and live in-flight counts from both memoizers (L2 slice, L1
processed grid). Per-product in-flight breakdown is computed by mapping each
in-flight key's store_url back to its product id — useful for spotting one
product saturating S3 fetches.

Heavy work (filesystem walks) runs via ``asyncio.to_thread`` so the event loop
stays free; the response itself is small.
"""

import asyncio

from fastapi import APIRouter

from constants import PRODUCTS
from services.data_renderer import clear_processed_cache, processed_memo_stats
from services.disk_cache import collect_disk_stats, get_refresh_status
from services.loader import clear_slice_cache, slice_memo_stats

router = APIRouter()


def _inflight_by_product(
    inflight_keys: list, product_index: dict[tuple[str, tuple[str, ...]], str]
) -> dict[str, int]:
    """Group in-flight keys by product id via (store_url, sorted-variables).

    Two distinct products can share a source_path (e.g. UV currents and SLA both
    served from the same Zarr), so store_url alone is ambiguous. The slice key's
    variables are already sorted (services/loader.py); the processed key's are
    not (services/data_renderer.py) — sort defensively here.
    """
    counts: dict[str, int] = {}
    for key in inflight_keys:
        pid = product_index.get((key[0], tuple(sorted(key[2]))))
        if pid is not None:
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def _build_response() -> dict:
    slice_stats = slice_memo_stats()
    processed_stats = processed_memo_stats()

    product_index = {
        (p.source_path, tuple(sorted(p.variables))): pid for pid, p in PRODUCTS.items()
    }
    slice_inflight_by_pid = _inflight_by_product(slice_stats["inflight_keys"], product_index)
    processed_inflight_by_pid = _inflight_by_product(
        processed_stats["inflight_keys"], product_index
    )

    disk_stats = collect_disk_stats(list(PRODUCTS.values()))
    products = {
        pid: {
            "disk": disk_stats["per_product"][pid],
            "in_flight": {
                "slice": slice_inflight_by_pid.get(pid, 0),
                "processed": processed_inflight_by_pid.get(pid, 0),
            },
        }
        for pid in PRODUCTS
    }

    return {
        "disk": disk_stats["global"],
        "refresh": get_refresh_status(),
        "in_flight": {
            "slice": {
                "current": slice_stats["inflight"],
                "peak": slice_stats["peak_inflight"],
                "total_computes": slice_stats["total_computes"],
            },
            "processed": {
                "current": processed_stats["inflight"],
                "peak": processed_stats["peak_inflight"],
                "total_computes": processed_stats["total_computes"],
            },
        },
        "memory_cache": {
            "slice": {"size": slice_stats["cache_size"], "max": slice_stats["cache_max"]},
            "processed": {
                "size": processed_stats["cache_size"],
                "max": processed_stats["cache_max"],
            },
        },
        "products": products,
    }


@router.get(
    "/cache",
    summary="Cache state snapshot for debugging",
    description=(
        "Returns per-product disk-cache footprint, global disk usage vs eviction threshold, "
        "the most recent refresh-loop status, and live in-flight compute counts (current + "
        "peak-since-startup) for both the slice and processed-grid memoizers. In-flight counts "
        "are instantaneous — they reflect ongoing work at the moment of the request, not a "
        "rolling window."
    ),
)
async def get_cache_state():
    return await asyncio.to_thread(_build_response)


@router.delete(
    "/cache/memory",
    summary="Clear all in-memory caches",
    description=(
        "Drops every entry in the L2 slice cache and the L1 processed-grid cache. "
        "Disk cache is untouched. In-flight computes are not cancelled — they'll just "
        "miss the cache on completion and re-populate it."
    ),
)
async def clear_memory_cache():
    return {
        "cleared": {
            "slice": clear_slice_cache(),
            "processed": clear_processed_cache(),
        }
    }
