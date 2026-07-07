"""Read-only cache state for debugging production behaviour.

Combines live in-flight counts from both memoizers (L2 slice, L1 processed
grid). Per-product in-flight breakdown is computed by mapping each in-flight
key's store_url back to its product id — useful for spotting one product
saturating S3 fetches.
"""

from fastapi import APIRouter

from app.schemas.admin import CacheStateResponse, MemoryClearedResponse
from app.services.caching.processed_cache import clear_processed_cache, processed_memo_stats
from app.services.caching.slice_cache import clear_slice_cache, slice_memo_stats
from app.services.product.registry import iter_product_items

router = APIRouter()


def _inflight_by_product(
    inflight_keys: list, product_index: dict[tuple[str, tuple[str, ...]], str]
) -> dict[str, int]:
    """Group in-flight keys by product id via (store_url, sorted-variables).

    Two distinct products can share a source_path (e.g. UV currents and SLA both
    served from the same Zarr), so store_url alone is ambiguous. The slice key's
    variables are already sorted (services/caching/slice_cache.py); the processed key's are
    not (services/rendering/data_tiles.py) — sort defensively here.
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

    # iter_product_items returns a snapshot so a concurrent load_products() reload
    # can't raise "dictionary changed size during iteration" mid-comprehension.
    items = iter_product_items()
    product_index = {(p.source_path, tuple(sorted(p.variables))): pid for pid, p in items}
    slice_inflight_by_pid = _inflight_by_product(slice_stats["inflight_keys"], product_index)
    processed_inflight_by_pid = _inflight_by_product(
        processed_stats["inflight_keys"], product_index
    )

    products = {
        pid: {
            "slice_in_flight": slice_inflight_by_pid.get(pid, 0),
            "processed_in_flight": processed_inflight_by_pid.get(pid, 0),
        }
        for pid, _ in items
    }

    return {
        "in_flight": {
            "slice": {  # L2 in-memory slice cache (services/caching/slice_cache.py)
                "current": slice_stats["inflight"],
                "peak": slice_stats["peak_inflight"],
                "total_computes": slice_stats["total_computes"],
            },
            "processed": {  # L1 processed grid cache (services/caching/processed_cache.py)
                "current": processed_stats["inflight"],
                "peak": processed_stats["peak_inflight"],
                "total_computes": processed_stats["total_computes"],
            },
        },
        "memory_cache": {
            "slice": {"size": slice_stats["cache_size"], "max": slice_stats["cache_max"]},  # L2
            "processed": {  # L1
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
        "Returns live in-flight compute counts (current + peak-since-startup) for both the "
        "slice and processed-grid memoizers, per product. In-flight counts are instantaneous — "
        "they reflect ongoing work at the moment of the request, not a rolling window."
    ),
    response_model=CacheStateResponse,
    response_model_exclude_unset=True,
)
def get_cache_state():
    return CacheStateResponse.model_validate(_build_response())


@router.delete(
    "/cache/memory",
    summary="Clear all in-memory caches",
    description=(
        "Drops every entry in the L2 slice cache and the L1 processed-grid cache. "
        "In-flight computes are not cancelled — they'll just miss the cache on "
        "completion and re-populate it."
    ),
    response_model=MemoryClearedResponse,
)
def clear_memory_cache():
    # Sync def: the eviction work is dict pops under a threading.Lock (microseconds for
    # cache sizes ≤50). FastAPI dispatches sync handlers to the thread pool, so the loop
    # stays free without us having to wrap in to_thread.run_sync.
    return MemoryClearedResponse(slice=clear_slice_cache(), processed=clear_processed_cache())
