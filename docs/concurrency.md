# Concurrency

## Model

FastAPI runs `def` route handlers in a thread pool managed by anyio. Each concurrent request gets its own thread — the same one-thread-per-request model as Spring/Tomcat, but with a smaller default pool.

Each request for a tile falls into one of three paths:

- **Hot** — `(product, date)` already in the in-memory slice cache. The thread does chunk extraction and PNG encoding only — no S3 or disk I/O.
- **Disk warm** — `(product, date)` not in memory but present on disk. The thread reads and decompresses the LZ4-pickled slice from local EBS, populates the in-memory cache, then encodes the PNG. No S3 I/O.
- **Cold** — `(product, date)` not in memory or on disk. The thread fetches Zarr chunks from S3 (`.compute()`), decompresses them, writes the result to both the disk cache and the in-memory cache.

All three paths share the same thread pool, so they compete for the same slots. Disk warm and cold requests are the most expensive; hot requests return quickly and release their slot.

---

## Configuration

### Thread pool and memory caches

Three env vars form a consistent sizing chain. If you raise `THREAD_POOL_SIZE`, raise the other two proportionally.

| Env var                | Default | Rule                                                                                                      |
| ---------------------- | ------- | --------------------------------------------------------------------------------------------------------- |
| `THREAD_POOL_SIZE`     | `100`   | Max concurrent requests. Raise if you observe queuing under high load.                                    |
| `SLICE_CACHE_SIZE`     | `100`   | Keep ≥ `THREAD_POOL_SIZE` so a burst of cold requests does not immediately evict freshly computed slices. |
| `PROCESSED_CACHE_SIZE` | `400`   | Keep ≥ `SLICE_CACHE_SIZE × number_of_LOD_levels` (typically 3–5).                                         |

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

Without protection, concurrent requests for the same uncached `(product, date)` all see a cache miss and each launches its own `.compute()` — redundant S3 downloads proportional to the number of concurrent requesters.

`loader.py` uses a per-key `Future` (`_slice_in_flight`) to prevent this. The first thread to miss the cache creates the Future and does the `.compute()`; all other threads arriving for the same key during that window wait on `future.result()` instead of duplicating the work. Errors propagate to all waiting threads, and the in-flight entry is always cleaned up so a failed request does not permanently block future attempts.

The same pattern is applied to store opens (`_store_in_flight`) so two requests for different store URLs arriving simultaneously do not block each other.

> **Note:** Waiting threads still consume a thread slot. If 10 requests arrive for the same cold slice, 1 thread fetches and 9 block — all 10 slots are occupied until the fetch completes. This is unavoidable without a dedicated waiting queue, but the impact is short-lived given the ~200ms–2s cold duration on EC2.

---

## CloudFront and real-world concurrency

In production, CloudFront sits in front of this server and caches tile responses at the edge. A tile URL (`/visual_tiles/{product}/{date}/tiles/{z}/{x}/{y}.png`) is fully deterministic — the same URL always returns the same bytes for a given product and date — so CloudFront's cache hit rate is very high once a date has been requested.

In practice this means:

- The vast majority of tile requests are served by CloudFront and never reach the origin server.
- Only cache misses (first request for a tile coordinate, or after CloudFront TTL expiry) hit the origin.
- The thread pool and stampede protection described above are a backstop for origin misses, not the steady-state load path.

Concurrency pressure on the origin is therefore much lower than the theoretical maximums above suggest.
