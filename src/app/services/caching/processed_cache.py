"""L1 cache for processed (resampled + normalised) grids.

The processed grid for one (product, date, LOD) is shared across every tile in
that LOD — N×M tiles draw from the same numpy arrays. This cache holds those
arrays so the resample + normalize cost is paid once per LOD instead of per
tile.

Exposes the CacheBackend so the rendering pipeline can call ``get_or_compute``
directly. In-process dedup (independent of ``CACHE_BACKEND``) is a separate
concern that lives with its one consumer — see ``rendering/data_tiles.py``.
"""

import os

from app.services.caching.backend_factory import create_memoizer
from app.services.caching.memoizer import CacheBackend

# TTL for L1 entries when CACHE_BACKEND=redis (unused for "none"). Entries serve
# the tile-burst for one (product, date, LOD); a stationary session >TTL incurs
# one re-resample on the next request — ~10-50 ms, negligible vs steady-RAM savings.
_PROCESSED_CACHE_TTL = int(os.environ.get("PROCESSED_CACHE_TTL_SECONDS", 600))
# Backend is selectable via CACHE_BACKEND (redis/none, see backend_factory) so
# ECS instances can share L1 through Redis instead of each holding a private copy.
processed_memo: CacheBackend = create_memoizer(namespace="l1", ttl_seconds=_PROCESSED_CACHE_TTL)
