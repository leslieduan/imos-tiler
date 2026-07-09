"""L2 cache for fully-computed (store, date, variables) slices.

Exposes the CacheBackend so slice loading can call ``get_or_compute``
directly. In-process dedup (independent of ``CACHE_BACKEND``) is a separate
concern that lives with its one consumer — see ``store/slice_loader.py``.
"""

from app.config import settings
from app.services.caching.backend_factory import create_memoizer
from app.services.caching.memoizer import CacheBackend

# TTL for L2 entries when CACHE_BACKEND=redis (unused for "none"). L2 entries are
# useful for the lifetime of one active map view: a burst of tile requests for the
# same (product, date) lands within seconds, then the user moves on.
# Backend is selectable via CACHE_BACKEND (redis/none, see backend_factory) so
# multiple instances can share L2 through Redis instead of each holding a private copy.
slice_memo: CacheBackend = create_memoizer(
    namespace="l2", ttl_seconds=settings.SLICE_CACHE_TTL_SECONDS
)
