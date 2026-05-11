# Concurrency

## Model

FastAPI runs `def` route handlers in a thread pool managed by anyio. Each concurrent request gets its own thread — the same one-thread-per-request model as Spring/Tomcat, but with a smaller default pool.

Each request for a tile falls into one of two paths:

- **Cold** — `(product, date)` not in the slice cache. The thread fetches Zarr chunks from S3 (`.compute()`), decompresses them, and writes the result to the cache.
- **Warm** — `(product, date)` already cached. The thread does chunk extraction and PNG encoding in memory — no S3 I/O.

Both paths share the same thread pool, so cold and warm requests compete for the same slots.

---

## Configuration

Three env vars form a consistent sizing chain. If you raise `THREAD_POOL_SIZE`, raise the other two proportionally.

| Env var | Default | Rule |
|---|---|---|
| `THREAD_POOL_SIZE` | `100` | Max concurrent requests. Raise if you observe queuing under high load. |
| `SLICE_CACHE_SIZE` | `100` | Keep ≥ `THREAD_POOL_SIZE` so a burst of cold requests does not immediately evict freshly computed slices. |
| `PROCESSED_CACHE_SIZE` | `400` | Keep ≥ `SLICE_CACHE_SIZE × number_of_LOD_levels` (typically 3–5). |

---

## Capacity (deployed on EC2/ECS, same AWS region as S3)

S3 latency from within the same AWS region is an internal network hop — effectively negligible compared to home internet. The dominant cost on a cold request is chunk decompression and numpy assembly, not network wait.

### Cold requests

| Factor | Value |
|---|---|
| Request duration | ~100–300ms |
| Max simultaneous unique slices | 100 (thread pool limit) |
| Throughput | ~100 ÷ 0.2s ≈ **500 unique slices/s** |
| Bottleneck | CPU (decompression + numpy) and thread pool |

### Warm requests

| Factor | Value |
|---|---|
| Request duration | ~10–50ms |
| Max simultaneous | 100 (thread pool limit) |
| Throughput | ~100 ÷ 0.03s ≈ **3,000 req/s** |
| Bottleneck | CPU (PNG encode) and thread pool |

---

## Stampede protection

Without protection, concurrent requests for the same uncached `(product, date)` all see a cache miss and each launches its own `.compute()` — redundant S3 downloads proportional to the number of concurrent requesters.

`loader.py` uses a per-key `Future` (`_slice_in_flight`) to prevent this. The first thread to miss the cache creates the Future and does the `.compute()`; all other threads arriving for the same key during that window wait on `future.result()` instead of duplicating the work. Errors propagate to all waiting threads, and the in-flight entry is always cleaned up so a failed request does not permanently block future attempts.

The same pattern is applied to store opens (`_store_in_flight`) so two requests for different store URLs arriving simultaneously do not block each other.

> **Note:** Waiting threads still consume a thread slot. If 10 requests arrive for the same cold slice, 1 thread fetches and 9 block — all 10 slots are occupied until the fetch completes. This is unavoidable without a dedicated waiting queue, but the impact is short-lived given the ~100–300ms cold duration on EC2.

---

## Scaling beyond a single instance

The current design is single-process and in-memory. To scale horizontally (multiple EC2/ECS instances):

- The slice and processed caches are **not shared** across instances — each instance warms its own cache independently.
- A shared cache layer (Redis, ElastiCache) would eliminate redundant S3 fetches across instances, but adds operational complexity and is only worth considering once a single instance is genuinely saturated.
