"""L1 cache for processed (resampled + normalised) grids.

The processed grid for one (product, date, LOD) is shared across every tile in
that LOD — N×M tiles draw from the same numpy arrays. This cache holds those
arrays so the resample + normalize cost is paid once per LOD instead of per
tile.

Exposes the Memoizer so the rendering pipeline can call ``get_or_compute``
directly.
"""

import os

from cachetools import TTLCache

from app.utils.memoizer import Memoizer

_PROCESSED_CACHE_SIZE = int(os.environ.get("PROCESSED_CACHE_SIZE", 50))
# L1 entries serve the tile-burst for one (product, date, LOD); same idle-RAM
# argument as L2 (see services/caching/slice_cache.py). TTL is insertion-based, so a
# stationary session >TTL incurs one re-resample on the next request — ~10-50 ms,
# negligible vs the steady-RAM savings during idle periods.
_PROCESSED_CACHE_TTL = int(os.environ.get("PROCESSED_CACHE_TTL_SECONDS", 600))
_processed_cache: TTLCache = TTLCache(maxsize=_PROCESSED_CACHE_SIZE, ttl=_PROCESSED_CACHE_TTL)
processed_memo: Memoizer = Memoizer(_processed_cache)
