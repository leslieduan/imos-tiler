"""L1 cache for processed (resampled + normalised) grids.

The processed grid for one (product, date, LOD) is shared across every tile in
that LOD — N×M tiles draw from the same numpy arrays. This cache holds those
arrays so the resample + normalize cost is paid once per LOD instead of per
tile.

Exposes the Memoizer so the rendering pipeline can call ``get_or_compute``
directly; eviction + stats live here as the canonical owners of the cache
state.
"""

import logging
import os

from cachetools import TTLCache

from app.services.product.product import Product
from app.utils.memoizer import Memoizer

logger = logging.getLogger(__name__)

_PROCESSED_CACHE_SIZE = int(os.environ.get("PROCESSED_CACHE_SIZE", 50))
# L1 entries serve the tile-burst for one (product, date, LOD); same idle-RAM
# argument as L2 (see services/caching/slice_cache.py). TTL is insertion-based, so a
# stationary session >TTL incurs one re-resample on the next request — ~10-50 ms,
# negligible vs the steady-RAM savings during idle periods.
_PROCESSED_CACHE_TTL = int(os.environ.get("PROCESSED_CACHE_TTL_SECONDS", 600))
_processed_cache: TTLCache = TTLCache(maxsize=_PROCESSED_CACHE_SIZE, ttl=_PROCESSED_CACHE_TTL)
processed_memo: Memoizer = Memoizer(_processed_cache)


def processed_memo_stats() -> dict:
    """In-flight + LRU stats for the L1 processed-grid memoizer. Used by /admin/cache."""
    return {
        **processed_memo.stats(),
        "cache_size": len(_processed_cache),
        "cache_max": _processed_cache.maxsize,
    }


def evict_processed_cache(product: Product) -> None:
    vars_tuple = tuple(product.variables)
    removed = processed_memo.evict_matching(
        # Identify product by source_path and variables;
        lambda k: k[0] == product.source_path and k[2] == vars_tuple
    )
    if removed:
        logger.info(
            "Processed cache evicted for product",
            extra={"product_id": product.id, "entries_removed": removed},
        )


def clear_processed_cache() -> int:
    """Drop every entry in the L1 processed-grid cache. Returns count removed."""
    removed = processed_memo.evict_matching(lambda _: True)
    if removed:
        logger.info("Processed cache cleared", extra={"entries_removed": removed})
    return removed
