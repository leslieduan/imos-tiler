"""Cross-layer cache lifecycle: product-eviction fan-out.

This module owns the orchestration that touches every cache layer (L1 processed
grids, L2 slices) when a product is deregistered. Those layers do **not**
depend on each other — that one-way arrow is the reason this module exists.

The eviction function takes a single product to evict: L1 and L2 keys are
addressed by ``product.source_path`` + variables, so no other product's
entries are touched.
"""

import logging

from app.services.caching.processed_cache import evict_processed_cache
from app.services.caching.slice_cache import evict_slice_cache_for_product
from app.services.product.product import Product

logger = logging.getLogger(__name__)


def evict_product_cache(product: Product) -> None:
    """Remove all in-memory cache entries for a deleted product."""
    removed = evict_slice_cache_for_product(product)
    if removed:
        logger.info(
            "Memory cache evicted for product",
            extra={"product_id": product.id, "slices_removed": removed},
        )

    evict_processed_cache(product)
