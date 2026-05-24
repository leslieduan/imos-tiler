"""Concurrent-request dedup + optional caching.

Two patterns recur in this project:
  (a) cache + dedup — concurrent callers requesting the same key share a single
      computation, and the result is stored in an LRU for later reuse
      (services.caching.slice_cache.load_slice, services.rendering.data_tiles._get_processed).
  (b) dedup only — there's no cache (e.g. results are too large or already cached
      downstream), but identical in-flight requests should still share work
      (routers.public.visual_tiles tile/bbox dedup).

Both shapes used to be inlined as the same ~25-line "check cache → create Future
→ fast-path wait → compute → publish → cleanup" boilerplate. This class is that
boilerplate, once.

Errors propagate to all concurrent waiters via the shared Future, and the
in-flight entry is removed in ``finally`` so a failed compute does not
permanently block subsequent attempts for the same key.

NOT a replacement for ``services.caching.slice_cache._get_store`` — that adds TTL +
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
        self._peak_inflight = 0
        self._total_computes = 0

    def get_or_compute(self, key: Hashable, factory: Callable[[], T]) -> T:
        """Return cached value, wait on an in-flight compute, or run ``factory()`` once.

        Must be called from a worker thread (sync ``def`` handler or
        ``anyio.to_thread.run_sync``), never directly from an ``async def``
        coroutine on the event loop. Waiters block on ``future.result()``,
        which is a blocking syscall — invoking this from the loop would
        freeze every other request behind a single in-flight compute.
        """
        should_compute = False
        with self._lock:
            if self.cache is not None and key in self.cache:
                return self.cache[key]  # type: ignore[no-any-return]
            if key in self._inflight:
                future = self._inflight[key]
            else:
                future = concurrent.futures.Future()
                self._inflight[key] = future
                self._total_computes += 1
                if len(self._inflight) > self._peak_inflight:
                    self._peak_inflight = len(self._inflight)
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

    def stats(self) -> dict:
        """Snapshot of current/peak in-flight count, total computes started, and live keys.

        ``inflight_keys`` is copied under the lock so callers can iterate without
        racing on insertions or removals. Counters reset only on process restart.
        """
        with self._lock:
            return {
                "inflight": len(self._inflight),
                "inflight_keys": list(self._inflight.keys()),
                "peak_inflight": self._peak_inflight,
                "total_computes": self._total_computes,
            }

    def evict_matching(self, predicate: Callable[[Hashable], bool]) -> int:
        """Remove all cache entries whose key satisfies ``predicate``. Returns count removed."""
        if self.cache is None:
            return 0
        with self._lock:
            keys = [k for k in self.cache if predicate(k)]
            for k in keys:
                del self.cache[k]
        return len(keys)
