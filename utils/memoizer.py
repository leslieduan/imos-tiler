"""Concurrent-request dedup + optional caching.

Two patterns recur in this project:
  (a) cache + dedup — concurrent callers requesting the same key share a single
      computation, and the result is stored in an LRU for later reuse
      (services.loader.load_slice, services.data_renderer._get_processed).
  (b) dedup only — there's no cache (e.g. results are too large or already cached
      downstream), but identical in-flight requests should still share work
      (routers.visual_tiles tile/bbox dedup).

Both shapes used to be inlined as the same ~25-line "check cache → create Future
→ fast-path wait → compute → publish → cleanup" boilerplate. This class is that
boilerplate, once.

Errors propagate to all concurrent waiters via the shared Future, and the
in-flight entry is removed in ``finally`` so a failed compute does not
permanently block subsequent attempts for the same key.

NOT a replacement for ``services.loader._get_store`` — that adds TTL +
stale-while-revalidate + background refresh on top of the dedup pattern, which
this helper deliberately does not model.
"""

import concurrent.futures
import threading
from collections.abc import Callable, Hashable, MutableMapping
from typing import TypeVar

T = TypeVar("T")


class Memoizer:
    def __init__(self, cache: MutableMapping | None = None) -> None:
        """`cache` is any MutableMapping (dict, cachetools.LRUCache, …) or None for dedup-only."""
        self.cache = cache
        self._lock = threading.Lock()
        self._inflight: dict[Hashable, concurrent.futures.Future] = {}

    def get_or_compute(self, key: Hashable, factory: Callable[[], T]) -> T:
        """Return cached value, wait on an in-flight compute, or run ``factory()`` once."""
        should_compute = False
        with self._lock:
            if self.cache is not None and key in self.cache:
                return self.cache[key]  # type: ignore[no-any-return]
            if key in self._inflight:
                future = self._inflight[key]
            else:
                future = concurrent.futures.Future()
                self._inflight[key] = future
                should_compute = True

        if not should_compute:
            return future.result()  # type: ignore[no-any-return]

        try:
            result = factory()
            if self.cache is not None:
                with self._lock:
                    self.cache[key] = result
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
        return result

    def evict_matching(self, predicate: Callable[[Hashable], bool]) -> int:
        """Remove all cache entries whose key satisfies ``predicate``. Returns count removed."""
        if self.cache is None:
            return 0
        with self._lock:
            keys = [k for k in self.cache if predicate(k)]
            for k in keys:
                del self.cache[k]
        return len(keys)
