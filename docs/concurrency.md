# Concurrency

This document focuses on the **concurrency model** and **per-request capacity** at the origin server. Capacity planning by product mix — cache sizing, instance class, disk volume — is in [`technical.md` §14](technical.md#14-capacity-and-resource-planning), and the same premise carries through here unchanged.

## Planning premise

The numbers below assume the size-class abstraction from [`docs/dataset.md`](dataset.md) and [`technical.md` §14.1](technical.md#141-planning-premise--what-kinds-of-products-do-we-plan-for):

- **GSLA-class** product — ~351 × 641 grid, ~2 MB / variable slice in RAM, ~0.5 MB / date on disk (lz4). Single LOD entry ~1.4 MB.
- **Satellite-class** product — ~2000 × 3900 grid, ~61 MB slice in RAM, ~18 MB / date on disk (lz4). Four LOD entries summing to ~58 MB.

The dataset.md products are **representative anchors**, not an exhaustive list — production registers products at runtime via the admin API, and real products are expected to stay close in shape and scale to the anchors. Three planning scenarios are referenced throughout: **A (6 products: 2 GSLA + 4 satellite)**, **B (20 products: 6 GSLA + 14 satellite)**, **C (50 products: 10 GSLA + 40 satellite)**, with `CACHE_DAYS ∈ {30, 60, 90}`.

---

## Model

FastAPI runs `def` route handlers in a thread pool managed by anyio. Each concurrent request gets its own thread — the same one-thread-per-request model as Spring/Tomcat, but with a smaller default pool.

### Data tile paths (`/data_tiles/...`)

`load_slice` is lazy — the route handler passes a callable to `render_tile`, which only invokes it if `_get_processed` misses. Each request falls into one of these paths:

- **Processed warm** — `(product, date, lod)` already in `_processed_cache`. The thread does `_extract_chunk` + PNG encode only — no S3, disk, or slice I/O at all.
- **Slice warm** — `_processed_cache` misses; `(product, date)` is in the L2 slice cache. The thread loads `ds` from memory, resamples, populates `_processed_cache`, then encodes the tile.
- **Disk warm** — `_processed_cache` and L2 both miss; `(product, date)` is on disk. The thread reads + decompresses the lz4 pickle (~30ms), resamples, populates both caches, then encodes.
- **Cold** — nothing cached. The thread fetches Zarr chunks from S3 (`.compute()`, ~2s), writes to disk and L2, resamples, populates `_processed_cache`, then encodes.

### Visual tile paths (`/visual_tiles/...` and `/bbox`)

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
| `SLICE_CACHE_SIZE`     | `10`    | LRU size for the L2 in-memory slice cache. Satellite-class slice ≈ 61 MB; GSLA-class slice ≈ 2 MB. **The default is too small for any scenario in [§14.7](technical.md#147-planning-scenarios)** — size as `N_products × hot_dates_per_product` (recommended 3). |
| `PROCESSED_CACHE_SIZE` | `50`    | LRU size for the L1 processed-grid cache. Sized as `SLICE_CACHE_SIZE × LOD.max_lods (4)` with headroom. Satellite-class LOD-4 entry ≈ 41 MB. |

### Store TTL

| Env var             | Default | Description                                                                                                                                                                              |
| ------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `STORE_TTL_SECONDS` | `600`   | How long a Zarr store handle is considered fresh. After expiry the stale store is served immediately while a background thread re-opens it — requests never block waiting for a refresh. |

### Disk cache

| Env var                          | Default   | Description                                                                                                                                |
| -------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `DISK_CACHE_PATH`                | _(unset)_ | Absolute path for the disk cache directory. Disk caching is disabled when unset.                                                           |
| `DISK_CACHE_LIMIT_GB`            | `20`      | Maximum disk usage in GB before eviction runs. **Default is too small for almost any production scenario — see [`technical.md` §14.7](technical.md#147-planning-scenarios) for recommended values per scenario.** |
| `DISK_EVICTION_THRESHOLD`        | `0.85`    | Eviction kicks in when usage exceeds this fraction of the limit (i.e. 17 GB at the default 20 GB limit). Smallest files are evicted first. |
| `CACHE_DAYS`                     | `30`      | Number of most-recent dates to keep on disk per product. Older dates are removed during the refresh cycle. Project plans to support up to **90** (3-month history). |
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

| Factor           | Value                                                              |
| ---------------- | ------------------------------------------------------------------ |
| Request duration | ~50 ms (GSLA-class) — ~200 ms (satellite-class)                    |
| Max simultaneous | 100 (thread pool limit)                                            |
| Throughput burst | ~2,000 req/s (GSLA-class) — ~500 req/s (satellite-class)           |
| Bottleneck       | EBS read + lz4 decompress + numpy resample                         |

Sustained throughput on satellite-class slices is further capped by **EBS bandwidth**: gp3 baseline 125 MB/s ÷ 18 MB/slice ≈ ~7 unique satellite-slice loads/sec. Provision EBS IOPS / bandwidth above baseline if disk-warm becomes the dominant traffic pattern (rare in practice — repeated reads of the same slice promote it to L2 after the first hit, after which the request is hot).

### Cold requests (S3)

| Factor                       | Value                                                          |
| ---------------------------- | -------------------------------------------------------------- |
| Request duration             | ~400 ms (GSLA-class) — ~1.5–2 s (satellite-class)              |
| Max simultaneous cold slices | 100 (thread pool limit; deduplicated by `_slice_in_flight`)    |
| Throughput burst             | ~250 req/s (GSLA-class) — ~50–70 req/s (satellite-class)       |
| Bottleneck                   | S3 fetch + CPU (decompression + numpy resample)                |

The dominant cost in cold-class is the **S3 fetch itself** (~300–800 ms per Zarr chunk; the satellite-class slice needs 6 chunks). Disk-warm is ~5–10× faster than cold because the same 18 MB satellite slice reads from local EBS (~5–25 ms) instead of S3, and the slice is stored fully assembled rather than as 6 separate Zarr chunks that must be re-combined.

In practice, cold S3 requests only occur for dates older than `CACHE_DAYS` (outside the disk cache window) or before the startup prewarm completes. The hot/disk-warm/cold capacity numbers above are **per-request** and independent of product mix — they hold for Scenario A, B, and C alike. What changes across scenarios is the **hit-rate distribution**: a larger product mix increases the chance that any given request falls into a cold or disk-warm tier rather than the hot tier, which is why [`technical.md` §14.3](technical.md#143-why-the-default-slice_cache_size10-is-too-small-for-production) sizes cache capacity to keep the working set hot for each scenario.

### Scaling `THREAD_POOL_SIZE`

The throughput numbers above are **burst ceilings** computed as `THREAD_POOL_SIZE ÷ request_duration` at `THREAD_POOL_SIZE = 100`. They represent what the pool can absorb in a brief spike, **not what the server can sustain indefinitely**. Sustained throughput is bound by real resources (CPU cores, EBS bandwidth, S3 connection pool) that don't scale with thread count.

**What scales linearly with `THREAD_POOL_SIZE`, and what doesn't:**

| Resource                                | Scales with pool size?   | Actual ceiling                                                                              |
| --------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------- |
| Burst capacity (short spikes)           | **Yes**, linearly        | Transient RAM: `pool_size × 61 MB` worst-case unique satellite slices in flight             |
| Hot sustained throughput                | **No**                   | CPU. On 4 vCPU with GIL-releasing PNG encode, plateaus around **~250–400 req/s** regardless of pool size |
| Disk-warm sustained (satellite-class)   | **No**                   | EBS bandwidth: gp3 baseline 125 MB/s ÷ 18 MB ≈ **~7 unique slices/sec**                     |
| Cold sustained (satellite-class)        | Partially                | S3 connection pool (aiobotocore default ~10 per host) + CPU for decompress/assembly         |
| Queueing tolerance under burst          | **Yes**, linearly        | OS thread limit (Linux defaults: thousands per process)                                     |

**Throughput at higher `THREAD_POOL_SIZE` (burst ceilings):**

| `THREAD_POOL_SIZE` | Hot burst        | Disk-warm satellite burst | Cold satellite burst | Worst-case transient RAM | Thread-stack RAM |
| ------------------ | ---------------- | ------------------------- | -------------------- | ------------------------ | ---------------- |
| 50                 | ~1,500 req/s     | ~250 req/s                | ~25–35 req/s         | ~3 GB                    | ~50 MB           |
| **100** (default)  | ~3,000 req/s     | ~500 req/s                | ~50–70 req/s         | ~6 GB                    | ~100 MB          |
| 200                | ~6,000 req/s     | ~1,000 req/s              | ~100–140 req/s       | ~12 GB                   | ~200 MB          |
| 500                | ~15,000 req/s    | ~2,500 req/s              | ~250–350 req/s       | ~30 GB                   | ~500 MB          |

The burst columns scale linearly because they're arithmetic ceilings, not physical ones. **Sustained throughput converges to the CPU / EBS / S3 ceilings regardless of pool size** — raising the pool from 100 to 500 with 4 vCPU does not give 5× sustained hot throughput; it just lets bursts of 500 concurrent requests be absorbed without queueing rejections, at the cost of 5× transient RAM.

**Theoretical maximum?** `anyio.to_thread.current_default_thread_limiter().total_tokens` accepts any positive integer — anyio itself has **no hard cap**. The practical ceiling is what your OS + RAM + CPU support. Linux can run thousands of threads per process; the only hard limits are:

- **OS**: `ulimit -u` (max user processes) — usually thousands, configurable.
- **RAM stack**: ~1 MB per thread (Linux default `pthread` stack). 1000 threads ≈ 1 GB.
- **GIL** + **vCPU**: at most `N_cores × ~5` concurrent threads will produce real CPU throughput; the rest are blocked on I/O or context-switched.

**When to raise the pool size:**

- Nginx access logs show request latency spikes correlated with concurrent-request count → the pool is exhausted, raise it.
- Steady-state CPU is **< 70 %** on all cores while you observe queueing → the pool, not the CPU, is the bottleneck.
- CPU is pegged at **100 % across all cores** → the CPU is the bottleneck; raising the pool just adds context-switching overhead without throughput gain. Provision more vCPU or scale out horizontally instead.

For the production scenarios in [`technical.md` §14.7](technical.md#147-planning-scenarios), `THREAD_POOL_SIZE = 100` is sufficient when fronted by CloudFront, which absorbs the bulk of repeat traffic before it reaches the origin. Raise to 200 only when sized for a workload that legitimately produces simultaneous bursts of >100 unique uncached requests and you have the RAM headroom (see the transient-RAM column above).

---

## Stampede protection

Two layers of stampede protection prevent redundant recomputation.

**Slice layer** (`loader.py`, `_slice_in_flight`)

Without protection, concurrent requests for the same uncached `(product, date)` would each launch their own `.compute()` — redundant S3 downloads proportional to the number of concurrent requesters.

`_slice_in_flight` is a per-key `Future` dict. The first thread to miss the cache creates the Future and does the `.compute()`; all other threads arriving for the same key during that window wait on `future.result()` instead. This also limits peak in-flight memory to `unique_keys × slice_size` rather than `concurrent_requests × slice_size`. Errors propagate to all waiting threads, and the in-flight entry is always cleaned up.

The same pattern is applied to store opens (`_store_in_flight`) so two requests for different store URLs arriving simultaneously do not block each other.

**Processed grid layer** (`data_renderer.py`, `_processed_inflight`)

Concurrent requests for the same `(product, date, lod)` that all miss `_processed_cache` would each run the full resample. `_processed_inflight` is a per-key `concurrent.futures.Future` dict (same mechanism as the slice and store layers — see [`technical.md` §10.5](technical.md#105-stampede-protection)): the first thread creates the Future and runs `_compute_scalar`/`_compute_uv`; all others wait on `future.result()` and receive the cached result when it completes. Errors propagate to all waiting threads.

> **Note:** Waiting threads still consume a thread slot at both layers. If 10 requests arrive for the same cold slice, 1 thread fetches and 9 block — all 10 slots are occupied until the fetch completes. This is unavoidable without a dedicated waiting queue, but the impact is short-lived given the ~200ms–2s cold duration on EC2.

---

## CloudFront and real-world concurrency

In production, CloudFront sits in front of this server and caches tile responses at the edge. A tile URL (`/visual_tiles/{product}/{date}/{z}/{x}/{y}.png`) is fully deterministic — the same URL always returns the same bytes for a given product and date — so CloudFront's cache hit rate is very high once a date has been requested.

In practice this means:

- The vast majority of tile requests are served by CloudFront and never reach the origin server.
- Only cache misses (first request for a tile coordinate, or after CloudFront TTL expiry) hit the origin.
- The thread pool and stampede protection described above are a backstop for origin misses, not the steady-state load path.

Concurrency pressure on the origin is therefore much lower than the theoretical maximums above suggest.
