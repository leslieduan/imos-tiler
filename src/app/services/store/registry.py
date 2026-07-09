"""Per-URL registry of long-lived xarray.Dataset handles backed by Zarr stores.

Not a cache in the strict sense: handles are not evicted (the URL set is small
and bounded by registered products), and ``ttl`` triggers a background refresh
rather than expiry. Stale entries keep serving until the refresh completes, so
requests never block on freshness — only the very first open per URL blocks.

A per-store ``{local_date: [timestamps]}`` index is built alongside the dataset
so ``load_slice`` / ``get_available_dates`` can resolve a local date in O(1)
instead of converting every timestamp on the hot path.
"""

import asyncio
import concurrent.futures
import threading
import time
import traceback

import anyio
import xarray as xr

from app.config import settings
from app.config.constants import COORD_NAMES
from app.utils.dates import ts_to_local_date

_STORE_TTL = float(settings.STORE_TTL_SECONDS)

# Capacity gate for concurrent store opens during prewarm. Bounded to the S3
# connection ceiling, not CPU. Runs on the shared anyio pool but a separate
# budget so a many-product startup can't transiently consume tile-handler slots.
_STORE_PREWARM_LIMITER = anyio.CapacityLimiter(settings.STORE_PREWARM_WORKERS)

# Per-syscall timeouts on every S3 connection. Without these, a stuck socket can
# pin a worker thread indefinitely (Python threads can't be cancelled, so a
# request-level wait would free the request but leave the thread held until the
# kernel eventually times out — minutes under bad network conditions).
# Passed via `config_kwargs` (not `client_kwargs`): s3fs builds its own Config
# and passes it as `config=` to create_client, so a `config` key in client_kwargs
# collides with that positional and raises TypeError.
_S3_CONFIG_KWARGS = {
    "connect_timeout": settings.S3_CONNECT_TIMEOUT,
    "read_timeout": settings.S3_READ_TIMEOUT,
    "retries": {"max_attempts": settings.S3_MAX_ATTEMPTS, "mode": "standard"},
}


def _storage_options(store_url: str) -> dict:
    """Storage-backend options for fsspec/zarr, derived from the URL scheme.

    - ``s3://`` defaults to anonymous access (IMOS's AODN buckets are public). Set
      ``settings.S3_ANON = False`` to let fsspec discover AWS credentials via the
      standard chain (env vars → ``~/.aws/credentials`` → IAM role) — needed for
      private buckets.
    - Other schemes (``file://``, ``https://``, ``gs://``, plain paths …) pass no
      options; fsspec / its backend picks sensible defaults.
    """
    if store_url.startswith("s3://"):
        return {"anon": settings.S3_ANON, "config_kwargs": _S3_CONFIG_KWARGS}
    return {}


def _open_store(store_url: str) -> xr.Dataset:
    # chunks={} pins dask to the store's *native* on-disk chunking (one dask chunk
    # per zarr chunk). This is what the no-arg default already resolves to, but we
    # state it explicitly: the hot path is a single-time .sel(...).compute(), and
    # native chunks mean that read fetches only the time-block(s) it needs while
    # still letting dask fetch spatial chunks from S3 in parallel. Do NOT switch to
    # chunks='auto' — auto merges adjacent time-blocks into one dask chunk, turning
    # a one-slice read into a multi-slice S3 over-read. (Tests mock open_zarr with
    # numpy datasets, so such a regression would pass CI but degrade production.)
    ds = xr.open_zarr(store_url, chunks={}, storage_options=_storage_options(store_url))
    rename = {k: v for k, v in COORD_NAMES.items() if k in ds.dims or k in ds.coords}
    if rename:
        ds = ds.rename(rename)
    if "lat" not in ds.dims or "lon" not in ds.dims:
        raise ValueError(
            f"Store {store_url!r} missing lat/lon dims after rename (found: {list(ds.dims)})"
        )
    if "time" in ds.dims:
        ds = ds.sortby("time")
    return ds


def _build_date_index(ds: xr.Dataset) -> dict[str, list]:
    """Return {local_date: [timestamps]} for the dataset's time coord, or {} if missing."""
    if "time" not in ds.dims:
        return {}
    index: dict[str, list] = {}
    for ts in ds.coords["time"].values:
        index.setdefault(ts_to_local_date(ts), []).append(ts)
    return index


class StoreRegistry:
    """See module docstring for the design.

    Concurrent first-time opens of the *same* URL share one ``xr.open_zarr`` call
    via a per-URL ``concurrent.futures.Future``; opens of *different* URLs run in
    parallel (the original implementation serialised them under a single global
    lock until this pattern was introduced).
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._stores: dict[str, xr.Dataset] = {}
        self._opened_at: dict[str, float] = {}
        self._refreshing: set[str] = set()
        self._in_flight: dict[str, concurrent.futures.Future] = {}
        self._date_index: dict[str, dict[str, list]] = {}
        self._lock = threading.Lock()

    def get(self, store_url: str) -> xr.Dataset:
        """Return the dataset for ``store_url``, opening it on first request."""
        should_open = False
        with self._lock:
            if store_url in self._stores:
                if time.monotonic() - self._opened_at[store_url] < self._ttl:
                    return self._stores[store_url]
                # TTL expired — return stale store and trigger a background refresh.
                if store_url not in self._refreshing:
                    self._refreshing.add(store_url)
                    print(f"Store TTL expired, refreshing in background: {store_url}")
                    threading.Thread(
                        target=self._refresh_background, args=(store_url,), daemon=True
                    ).start()
                return self._stores[store_url]
            if store_url in self._in_flight:
                future = self._in_flight[store_url]
            else:
                future = concurrent.futures.Future()
                self._in_flight[store_url] = future
                should_open = True

        if not should_open:
            return future.result()

        try:
            ds = _open_store(store_url)
            index = _build_date_index(ds)
            self._publish(store_url, ds, index)
            print(f"Store opened: {store_url} (date_count={len(index)})")
            future.set_result(ds)
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            with self._lock:
                self._in_flight.pop(store_url, None)
        return ds

    def date_index(self, store_url: str) -> dict[str, list]:
        """Return the {local_date: [timestamps]} map for ``store_url`` (or empty dict)."""
        with self._lock:
            return self._date_index.get(store_url, {})

    async def prewarm(self, store_urls: list[str]) -> None:
        """Open every URL in parallel via the anyio thread pool.

        Moves the one-time S3 metadata cost from the first user request to server
        startup, and lets get_products_availability respond fast on first call.
        Per-URL failures are logged and swallowed so a single bad URL doesn't
        block the others.
        """

        async def _one(url: str) -> None:
            try:
                await anyio.to_thread.run_sync(self.get, url, limiter=_STORE_PREWARM_LIMITER)
            except Exception:
                print(f"Store prewarm failed: {url}")
                traceback.print_exc()

        await asyncio.gather(*(_one(url) for url in store_urls))

    def clear(self) -> None:
        """Drop all cached state. Intended for tests."""
        with self._lock:
            self._stores.clear()
            self._opened_at.clear()
            self._refreshing.clear()
            self._in_flight.clear()
            self._date_index.clear()

    def _publish(self, store_url: str, ds: xr.Dataset, index: dict[str, list]) -> None:
        """Atomically replace store, opened-at timestamp, and date index for a URL."""
        with self._lock:
            self._stores[store_url] = ds
            self._opened_at[store_url] = time.monotonic()
            self._date_index[store_url] = index

    def _refresh_background(self, store_url: str) -> None:
        try:
            ds = _open_store(store_url)
            index = _build_date_index(ds)
            self._publish(store_url, ds, index)
            print(f"Store refreshed: {store_url}")
        except Exception:
            print(f"Background refresh failed: {store_url}")
            traceback.print_exc()
        finally:
            with self._lock:
                self._refreshing.discard(store_url)


store_registry = StoreRegistry(_STORE_TTL)


def get_store(store_url: str) -> xr.Dataset:
    return store_registry.get(store_url)


def get_available_dates(store_url: str) -> list[str]:
    get_store(store_url)  # ensures the date index for this URL is populated
    index = store_registry.date_index(store_url)
    return sorted(index) if index else []


async def prewarm_stores(store_urls: list[str]) -> None:
    await store_registry.prewarm(store_urls)
