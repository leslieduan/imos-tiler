# Concurrency

## Model

FastAPI runs `def` route handlers in a thread pool managed by anyio. Each concurrent request gets its own thread — the same one-thread-per-request model as Spring/Tomcat, but with a smaller default pool.

### Data tile paths (`/data_tiles/.../tiles/`)

`load_slice` is lazy — the route handler passes a callable to `render_tile`, which only invokes it if `_get_processed` misses. Each request falls into one of these paths:

- **Processed warm** — `(product, date, lod)` already in `_processed_cache`. The thread does `_extract_chunk` + PNG encode only — no S3, disk, or slice I/O at all.
- **Slice warm** — `_processed_cache` misses; `(product, date)` is in the L2 slice cache. The thread loads `ds` from memory, resamples, populates `_processed_cache`, then encodes the tile.
- **Disk warm** — `_processed_cache` and L2 both miss; `(product, date)` is on disk. The thread reads + decompresses the lz4 pickle (~30ms), resamples, populates both caches, then encodes.
- **Cold** — nothing cached. The thread fetches Zarr chunks from S3 (`.compute()`, ~2s), writes to disk and L2, resamples, populates `_processed_cache`, then encodes.

### Visual tile paths (`/visual_tiles/.../tiles/` and `/bbox`)

No processed grid cache. Each request calls `load_slice` unconditionally:

- **L2 warm** — `(product, date)` in the L2 slice cache. The thread reads `ds` from memory and renders via `XarrayReader`.
- **Disk warm** — L2 miss; slice on disk. Reads + decompresses, populates L2, renders.
- **Cold** — fetches from S3, writes to disk and L2, renders.

All paths share the same thread pool and compete for the same slots. Processed-warm data tile requests are fastest and release their slot quickly; cold requests hold slots for seconds.

---

## Configuration

### Thread pool and memory caches

`THREAD_POOL_SIZE` is independent of the cache sizes. Cache sizing is driven by dataset slice size and the number of concurrently active dates.

| Env var                | Default | Rule                                                                                                                                  |
| ---------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `THREAD_POOL_SIZE`     | `100`   | Max concurrent requests. Raise if you observe queuing under high load.                                                                |
| `SLICE_CACHE_SIZE`     | `10`    | Number of concurrent active `(product, date)` pairs for visual_tiles. Each satellite heatwave slice is ~61 MB.                       |
| `PROCESSED_CACHE_SIZE` | `50`    | `SLICE_CACHE_SIZE × LOD.max_lods (4)` with headroom — keeps all LOD levels warm for every date in the L2 slice cache. Each satellite LOD 4 entry is ~41 MB. |

### Store TTL

| Env var             | Default | Description                                                                                                                                                                              |
| ------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `STORE_TTL_SECONDS` | `600`   | How long a Zarr store handle is considered fresh. After expiry the stale store is served immediately while a background thread re-opens it — requests never block waiting for a refresh. |

### Disk cache

| Env var                          | Default   | Description                                                                                                                                |
| -------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `DISK_CACHE_PATH`                | _(unset)_ | Absolute path for the disk cache directory. Disk caching is disabled when unset.                                                           |
| `DISK_CACHE_LIMIT_GB`            | `20`      | Maximum disk usage in GB before eviction runs.                                                                                             |
| `DISK_EVICTION_THRESHOLD`        | `0.85`    | Eviction kicks in when usage exceeds this fraction of the limit (i.e. 17 GB at the default 20 GB limit). Smallest files are evicted first. |
| `CACHE_DAYS`                     | `30`      | Number of most-recent dates to keep on disk per product. Older dates are removed during the refresh cycle.                                 |
| `PREWARM_WORKERS`                | `4`       | Thread pool size used during startup disk prewarming. Higher values speed up startup at the cost of more simultaneous S3 reads.            |
| `CACHE_REFRESH_INTERVAL_SECONDS` | `14400`   | How often (in seconds) the background refresh loop runs to add newly available dates and evict stale ones. Default is 4 hours.             |

---

## Capacity (deployed on EC2/ECS, same AWS region as S3)

S3 latency from within the same AWS region is an internal network hop — effectively negligible compared to home internet. The dominant cost on a cold request is chunk decompression and numpy assembly, not network wait.

### Hot requests

| Factor           | Value                            |
| ---------------- | -------------------------------- |
| Request duration | ~10–50ms                         |
| Max simultaneous | 100 (thread pool limit)          |
| Throughput       | ~100 ÷ 0.03s ≈ **3,000 req/s**   |
| Bottleneck       | CPU (PNG encode) and thread pool |

### Disk warm requests

| Factor           | Value                              |
| ---------------- | ---------------------------------- |
| Request duration | ~100–500ms                         |
| Max simultaneous | 100 (thread pool limit)            |
| Bottleneck       | Disk I/O (LZ4 decompress from EBS) |

### Cold requests (S3)

| Factor                       | Value                                       |
| ---------------------------- | ------------------------------------------- |
| Request duration             | ~200ms–2s                                   |
| Max simultaneous cold slices | 100 (thread pool limit)                     |
| Throughput                   | ~100 ÷ 0.2s ≈ **500 unique slices/s**       |
| Bottleneck                   | CPU (decompression + numpy) and thread pool |

In practice, cold S3 requests only occur for dates older than 30 days (outside the disk cache window) or before the startup prewarm completes.

---

## Stampede protection

Two layers of stampede protection prevent redundant recomputation.

**Slice layer** (`loader.py`, `_slice_in_flight`)

Without protection, concurrent requests for the same uncached `(product, date)` would each launch their own `.compute()` — redundant S3 downloads proportional to the number of concurrent requesters.

`_slice_in_flight` is a per-key `Future` dict. The first thread to miss the cache creates the Future and does the `.compute()`; all other threads arriving for the same key during that window wait on `future.result()` instead. This also limits peak in-flight memory to `unique_keys × slice_size` rather than `concurrent_requests × slice_size`. Errors propagate to all waiting threads, and the in-flight entry is always cleaned up.

The same pattern is applied to store opens (`_store_in_flight`) so two requests for different store URLs arriving simultaneously do not block each other.

**Processed grid layer** (`data_renderer.py`, `_processed_inflight`)

Concurrent requests for the same `(product, date, lod)` that all miss `_processed_cache` would each run the full resample. `_processed_inflight` uses a per-key `threading.Event` to ensure only the first thread runs `_compute_scalar`/`_compute_uv`; all others wait on the event and receive the cached result when it fires.

> **Note:** Waiting threads still consume a thread slot at both layers. If 10 requests arrive for the same cold slice, 1 thread fetches and 9 block — all 10 slots are occupied until the fetch completes. This is unavoidable without a dedicated waiting queue, but the impact is short-lived given the ~200ms–2s cold duration on EC2.

---

## CloudFront and real-world concurrency

In production, CloudFront sits in front of this server and caches tile responses at the edge. A tile URL (`/visual_tiles/{product}/{date}/tiles/{z}/{x}/{y}.png`) is fully deterministic — the same URL always returns the same bytes for a given product and date — so CloudFront's cache hit rate is very high once a date has been requested.

In practice this means:

- The vast majority of tile requests are served by CloudFront and never reach the origin server.
- Only cache misses (first request for a tile coordinate, or after CloudFront TTL expiry) hit the origin.
- The thread pool and stampede protection described above are a backstop for origin misses, not the steady-state load path.

Concurrency pressure on the origin is therefore much lower than the theoretical maximums above suggest.
