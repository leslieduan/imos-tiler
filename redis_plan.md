# Redis Slice Cache — Implementation Plan

## Problem

Cold large tile requests take up to ~1.8s because `load_slice` reads Zarr chunks from S3. The existing
in-memory `_slice_cache` eliminates repeat hits within one process, but is wiped on every
container restart or new ECS task deployment. CloudFront reduces origin load but has its own
TTL and eviction policy — tiles regularly cycle out of the CDN cache, and those misses flow
back to the origin, hitting the slice path again. The cold path therefore occurs both on
container restart and on any CloudFront eviction.

## Solution

Add Redis as a persistent L2 slice cache. Redis is populated exclusively by a startup
prewarm and a background refresh cron — **not** by request hits. The cron maintains a
fixed window of the latest 10 available dates per product: adding new dates as the store
grows, evicting dates that fall outside the window.

```
Request
  │
  ├─ L1 hit? (_slice_cache, in-memory)  → sub-ms
  │
  ├─ L2 hit? (Redis)                    → ~30ms  → populate L1
  │
  └─ L3 miss (S3 .compute())            → ~2s    → populate L1 only
                   ▲
     Written only by prewarm / cron
```

## Why \_slice_cache is still needed

`_processed_cache` in `data_renderer.py` is keyed by `(id(ds), lod)`. This relies on
`_slice_cache` returning the **same Python object** for repeated calls. Without L1,
every Redis deserialisation produces a new object with a different `id` → processed
cache never hits → every tile re-runs the full bilinear resampling.

L1 also avoids redundant Redis round-trips within a single map session (10–20 tile
requests for the same product+date).

## Redis data layout

| Key                                      | Type   | Value                                        | TTL                       |
| ---------------------------------------- | ------ | -------------------------------------------- | ------------------------- |
| `slice:{store_url}:{date}:{sorted_vars}` | String | `lz4.compress(pickle.dumps(xr.Dataset))`     | none (managed explicitly) |
| `slice_dates:{product_id}`               | Set    | cached date strings for this product         | none                      |

No TTL on slice keys — the cron manages eviction explicitly. This avoids a race where a
key expires between the Set membership check and the key read.

## Redis memory sizing

With lz4 compression, float64 ocean arrays (which contain large NaN land masks) compress
roughly 3–4×. Using a conservative 3.5× ratio and per-product `cache_days`:

| Product type | Grid | Vars | Raw/date | lz4/date | Days | Total |
|---|---|---|---|---|---|---|
| sea_level_anomaly | 351×641 | 1 | 1.7 MB | 0.5 MB | 30 | 15 MB |
| ocean_current | 351×641 | 2 | 3.4 MB | 1.0 MB | 30 | 30 MB |
| radar_wind | 74×102 | 1 | 0.06 MB | ~0 MB | 30 | 1 MB |
| satellite_ssta | 2000×3900 | 1 | 62 MB | 18 MB | 7 | 126 MB |
| **4 current products** | | | | | | **~172 MB** |
| **10 products (est.)** | | | | | | **~450 MB** |

Redis internal overhead adds ~30% on top of stored data. Recommended `maxmemory`:

| Deployment | Expected data | Recommended `maxmemory` |
|---|---|---|
| 4 current products | ~220 MB | **512mb** |
| 10 products | ~580 MB | **1gb** |

### What happens if Redis runs out of memory

Redis is purely in-memory — it cannot spill to disk. Two failure modes:

- **No `maxmemory` set (default):** Redis grows until the OS OOM-killer terminates the
  process. The container crashes, all cache is lost, every request falls back to S3 until
  Redis restarts and prewarming completes.
- **`maxmemory` set with `noeviction`:** writes are refused when full. Without our own
  eviction logic the prewarm/cron write would fail silently, leaving cache partially populated.

We use `noeviction` **plus** our own eviction function that runs before every cron write
cycle. This gives us full control over what gets removed.

## Custom eviction strategy

Redis built-in policies (`allkeys-lru`, `volatile-ttl`, etc.) have no concept of slice
size or product semantics. We implement eviction ourselves, called proactively by the cron
before adding new dates.

**Priority: small slices evicted first, older dates within same size group evicted first.**

Rationale: small-grid products (radar, sea-level) read fast enough from S3 that losing
their cache entry is low-cost. Large-grid products (satellite, 2000×3900) take ~2s from
S3 and benefit most from staying cached. Within the same product, the oldest date is least
likely to be requested again.

```
Eviction order (front = evicted first):
  ┌────────────────────────────────────────────────────────┐
  │ radar_wind   2025-01-01  (small,  oldest)             │
  │ radar_wind   2025-01-02                               │
  │ ...                                                   │
  │ sea_level    2025-01-01  (medium, oldest)             │
  │ sea_level    2025-01-02                               │
  │ ...                                                   │
  │ satellite    2025-01-01  (large,  oldest)  ← last     │
  └────────────────────────────────────────────────────────┘
```

**`_evict_redis_if_needed(r, products)`** — called at the top of each `refresh_redis_cache` run:

```python
def _evict_redis_if_needed(r: redis.Redis, products: list[Product]) -> None:
    info = r.info("memory")
    maxmemory = info.get("maxmemory", 0)
    if maxmemory == 0:
        return  # no limit configured, nothing to do
    used = info.get("used_memory", 0)
    threshold = int(maxmemory * float(os.environ.get("REDIS_EVICTION_THRESHOLD", 0.85)))
    if used <= threshold:
        return

    # Collect all cached entries: (actual_stored_bytes, date, product, redis_key)
    entries = []
    for product in products:
        variables = product.variable if isinstance(product.variable, list) else [product.variable]
        for date in {d.decode() for d in r.smembers(f"slice_dates:{product.id}")}:
            key = _redis_key(product.source_path, date, variables)
            size = r.memory_usage(key) or 0
            entries.append((size, date, product, key, variables))

    # Smallest size first, oldest date first within same size
    entries.sort(key=lambda e: (e[0], e[1]))

    for size, date, product, key, variables in entries:
        if used <= threshold:
            break
        r.delete(key)
        r.srem(f"slice_dates:{product.id}", date)
        used -= size
        logger.info("Redis evicted (memory pressure): %s / %s (%d KB)", product.id, date, size // 1024)
```

`r.memory_usage(key)` returns the exact bytes Redis uses for that key including its
internal overhead — more accurate than estimating from grid dimensions.

`REDIS_EVICTION_THRESHOLD` defaults to `0.85` (evict when above 85% of `maxmemory`),
giving headroom before the hard limit is hit.

## Files changed

### `constants.py`

Add `cache_days: int = 30` to the `Product` dataclass. Large-grid products
(e.g. satellite) should be registered with a smaller value (e.g. `cache_days=7`) to
keep Redis usage bounded.

```python
@dataclass(frozen=True)
class Product:
    id: str
    source_path: str
    variable: str | list[str] = ""
    lod_grids: dict[int, tuple[int, int]] = field(default_factory=dict)
    chunk_px: tuple[int, int] = CHUNK_PX
    padding: int = PADDING
    cache_days: int = 30          # ← new
```

### `pyproject.toml`

- Add `redis>=5.0` and `lz4>=4.0` to dependencies (`uv add redis lz4`).

### `docker-compose.yml`

- Add `redis:alpine` service with `restart: unless-stopped`.
- Add `REDIS_URL=redis://redis:6379` to the app environment.

### `services/loader.py`

**Redis client**

```python
_redis: redis.Redis | None = None

def init_redis() -> None:
    global _redis
    url = os.environ.get("REDIS_URL")
    if url:
        _redis = redis.from_url(url, socket_connect_timeout=2)
        _redis.ping()          # fail fast if misconfigured
        logger.info("Redis connected: %s", url)

def close_redis() -> None:
    if _redis:
        _redis.close()
```

**Key helper**

```python
def _redis_key(store_url: str, date: str, variables: list[str]) -> str:
    return f"slice:{store_url}:{date}:{','.join(sorted(variables))}"
```

**`load_slice` — add Redis L2 read**

After the L1 miss check and before creating the in-flight Future, probe Redis:

```python
r = _redis
if r:
    raw = r.get(_redis_key(store_url, date, variables))
    if raw:
        ds = pickle.loads(lz4.frame.decompress(raw))
        with _slice_lock:
            _slice_cache[cache_key] = ds
        return ds
```

No write-back from request path — Redis is written only by prewarm/cron.

**`prewarm_redis_slices(products, n_days=10)`**

Called once at startup in a background thread.

```python
def prewarm_redis_slices(products: list[Product]) -> None:
    r = _redis
    if not r:
        return
    for product in products:
        variables = product.variable if isinstance(product.variable, list) else [product.variable]
        dates = get_available_dates(product.source_path)[-product.cache_days:]
        for date in dates:
            try:
                ds = load_slice(product.source_path, date, variables)   # populates L1
                key = _redis_key(product.source_path, date, variables)
                if not r.exists(key):
                    r.set(key, lz4.frame.compress(pickle.dumps(ds)))
                    r.sadd(f"slice_dates:{product.id}", date)
                    logger.info("Prewarmed Redis: %s / %s", product.id, date)
            except Exception:
                logger.warning("Prewarm failed: %s / %s", product.id, date, exc_info=True)
```

**`refresh_redis_cache(products, n_days=10)`**

Called on a recurring schedule (cron thread). Adds newly available dates; evicts dates
that are no longer in the latest-N window.

```python
def refresh_redis_cache(products: list[Product]) -> None:
    r = _redis
    if not r:
        return
    _evict_redis_if_needed(r, products)   # ← custom eviction before adding new dates
    for product in products:
        variables = product.variable if isinstance(product.variable, list) else [product.variable]
        target_dates = set(get_available_dates(product.source_path)[-product.cache_days:])
        cached_dates = {d.decode() for d in r.smembers(f"slice_dates:{product.id}")}

        # Add new dates
        for date in target_dates - cached_dates:
            try:
                ds = load_slice(product.source_path, date, variables)
                r.set(_redis_key(product.source_path, date, variables), lz4.frame.compress(pickle.dumps(ds)))
                r.sadd(f"slice_dates:{product.id}", date)
                logger.info("Redis cache added: %s / %s", product.id, date)
            except Exception:
                logger.warning("Redis cache add failed: %s / %s", product.id, date, exc_info=True)

        # Evict stale dates
        for date in cached_dates - target_dates:
            r.delete(_redis_key(product.source_path, date, variables))
            r.srem(f"slice_dates:{product.id}", date)
            logger.info("Redis cache evicted: %s / %s", product.id, date)
```

**`_start_cache_refresh_cron(products, interval_seconds)`**

Background daemon thread that calls `refresh_redis_cache` on a fixed interval.

```python
def _start_cache_refresh_cron(products: list[Product], interval_seconds: int) -> None:
    def _loop():
        while True:
            time.sleep(interval_seconds)
            refresh_redis_cache(products)
    threading.Thread(target=_loop, daemon=True, name="redis-cache-refresh").start()
```

Interval controlled by `CACHE_REFRESH_INTERVAL_SECONDS` (default `3600`).

### `main.py`

```python
from services.loader import (
    prewarm_stores,
    init_redis,
    close_redis,
    prewarm_redis_slices,
    _start_cache_refresh_cron,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = int(os.environ.get("THREAD_POOL_SIZE", 100))
    load_products()
    load_colormaps()
    init_redis()                                          # connect (no-op if REDIS_URL unset)
    store_urls = list({p.source_path for p in PRODUCTS.values()})
    prewarm_stores(store_urls)
    products = list(PRODUCTS.values())
    threading.Thread(                                     # prewarm in background
        target=prewarm_redis_slices, args=(products,), daemon=True
    ).start()
    interval = int(os.environ.get("CACHE_REFRESH_INTERVAL_SECONDS", 3600))
    _start_cache_refresh_cron(products, interval)
    yield
    close_redis()
```

## Environment variables

| Variable                         | Default | Description                                                                                          |
| -------------------------------- | ------- | ---------------------------------------------------------------------------------------------------- |
| `REDIS_URL`                      | unset   | Redis connection URL. If unset, Redis is disabled; only L1 in-memory cache is used.                 |
| `CACHE_REFRESH_INTERVAL_SECONDS` | `3600`  | How often the cron re-checks available dates and syncs the Redis window.                             |
| `REDIS_EVICTION_THRESHOLD`       | `0.85`  | Fraction of `maxmemory` at which the cron triggers custom eviction before adding new dates.          |

## Local dev / EC2 (Docker Compose)

```yaml
services:
  redis:
    image: redis:alpine
    command: redis-server --maxmemory 512mb --maxmemory-policy noeviction
    restart: unless-stopped

  app:
    environment:
      - REDIS_URL=redis://redis:6379
      - CACHE_REFRESH_INTERVAL_SECONDS=3600
```

`noeviction` means Redis refuses writes (not crashes) when full. The prewarm/cron
catches the error and logs a warning; the system degrades to S3 rather than dying.
Raise `maxmemory` to `1gb` once 10 products are registered.

## ECS migration path

When moving to ECS, replace `REDIS_URL` with the ElastiCache endpoint. No code changes
needed. The Redis service is removed from the compose file.

## Sequence on container startup

```
1. init_redis()           — connect, ping
2. prewarm_stores()       — open Zarr metadata (background threads, per store URL)
3. prewarm_redis_slices() — background thread:
     for each product × last 10 dates:
       load_slice() → L1 hit if already warm, else S3 read → write to Redis
4. _start_cache_refresh_cron() — starts cron thread (fires every CACHE_REFRESH_INTERVAL_SECONDS)
```

First user request for a recently prewarmed date arrives after step 3 completes:
→ L1 hit (sub-ms) if same process, or Redis hit (~30ms) if new task.

## What is NOT changed

- `_processed_cache` and `_store_cache` are untouched.
- `_slice_in_flight` stampede prevention is untouched.
- Redis being unavailable never surfaces to a caller — all Redis calls are wrapped in
  try/except; the system degrades to L1 + S3.
