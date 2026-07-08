"""In-process in-flight request coalescing — no caching, nothing to evict."""

import concurrent.futures
import threading
from collections.abc import Callable, Hashable
from typing import TypeVar

T = TypeVar("T")


class Deduper:
    """Stops concurrent callers with the same key from redoing the same work.

    Content-agnostic: doesn't know or care what ``factory()`` does (S3 fetch,
    resample, render, ...). If a call for ``key`` is already in flight, later
    callers block and share its result instead of each running ``factory()``
    themselves. Must be driven from a worker thread (sync ``def`` handler or
    ``anyio.to_thread.run_sync``), never directly from an ``async def`` on the
    event loop — waiting on the result is a blocking call.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[Hashable, concurrent.futures.Future] = {}

    def dedupe(self, key: Hashable, factory: Callable[[], T]) -> T:
        should_compute = False
        with self._lock:
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
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
        return result
