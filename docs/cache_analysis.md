# Cache Design Analysis

> **Historical record.** This document analyses and justifies the on-disk L3 cache tier described below. That tier has since been **removed** — the server now caches only in-memory (L1 processed grids + L2 slices; see `docs/technical.md` §10). The analysis is preserved for the historical context behind that decision, not as a description of the current architecture.

## Problem

Cold tile requests for large dadaset take up to ~2s because `load_slice` reads Zarr chunks from S3. The in-memory `_slice_cache` eliminates repeat hits within one process lifetime, but is wiped on every container restart. CloudFront reduces origin load but has its own TTL and eviction — tiles regularly cycle out of the CDN cache and those misses flow back to the origin, hitting the cold S3 path again. The cold path therefore occurs both on container restart and on any CloudFront eviction.

The goal is a persistent L3 cache that survives restarts and reduces cold S3 reads to milliseconds.

---

## What Is Cached

The cache does not store the full Zarr dataset. Each cache entry is a single **2D lat×lon slice** for one specific (date, variable set) combination — the result of calling `.sel(time=t).compute()` on the store. For a product with a 351×641 grid and one variable, this is a numpy array of ~1.7 MB of float64 values. The Zarr store itself may contain years of daily data across hundreds of time steps; only the dates actively needed (the latest `CACHE_DAYS`) are ever fetched and cached.

**Serialisation and compression:** each slice is serialised with `pickle.dumps` and compressed with `lz4.frame.compress` before writing to disk. lz4 is a block compression algorithm optimised for speed over ratio — compress and decompress at ~500 MB/s on a single core, with typically 3–4× compression on float64 ocean arrays. Ocean grids contain large NaN land masks (contiguous regions of identical bit patterns) that compress extremely well, pushing effective ratios higher for products with significant land coverage.

**Per-product size after lz4 compression:**

| Product | Grid | Variables | Raw size | lz4 size | Compression ratio |
|---|---|---|---|---|---|
| sea_level_anomaly | 351×641 | 1 | 1.7 MB | ~0.5 MB | ~3.4× |
| ocean_current | 351×641 | 2 | 3.4 MB | ~1.0 MB | ~3.4× |
| radar_wind | 74×102 | 1 | 0.06 MB | ~0.02 MB | ~3× |
| satellite_ssta | 2000×3900 | 1 | 62 MB | ~18 MB | ~3.4× |

A disk read of a typical slice file (~0.5–18 MB) takes ~5ms on local SSD. Decompression adds ~25ms for the largest files. Total disk-warm latency is ~30ms — imperceptible to a map user compared to a 2s cold S3 read.

**Storage scales cheaply.** Even with many more products and a longer cache window, the total footprint remains easily manageable on a single EBS volume:

| Products | Dates cached | Est. total lz4 size | EBS gp3 cost/month |
|---|---|---|---|
| 4 (current) | 30 | ~600 MB | <$1 |
| 10 | 30 | ~1.5 GB | <$1 |
| 20 | 30 | ~3 GB | <$1 |
| 20 | 90 | ~9 GB | ~$1 |
| 50 | 90 | ~22 GB | ~$2 |

EBS gp3 costs $0.08/GB-month. Even at 50 products × 90 dates the total cache fits in a 30 GB volume costing ~$2.50/month — less than an hour of ElastiCache. This is the core reason disk was chosen over Redis: the dataset grows with more products and longer windows, and disk scales linearly at near-zero marginal cost whereas Redis requires a larger (and disproportionately more expensive) instance class for each step up.

---

## Options Evaluated

### Option 1 — Redis

Redis is an in-memory key-value store, commonly used as a distributed cache. The core appeal is sub-millisecond reads and sharing across multiple instances.

**How it would work:**

- Slices serialised with `lz4.frame.compress(pickle.dumps(ds))` and stored as Redis string keys
- Populated by startup prewarm and a background refresh cron only — not by request hits
- On L2 miss, probe Redis before going to S3

**Sizing analysis:**

| Product                | Grid      | Vars | Raw/date | lz4/date | 30 days     |
| ---------------------- | --------- | ---- | -------- | -------- | ----------- |
| sea_level_anomaly      | 351×641   | 1    | 1.7 MB   | 0.5 MB   | 15 MB       |
| ocean_current          | 351×641   | 2    | 3.4 MB   | 1.0 MB   | 30 MB       |
| radar_wind             | 74×102    | 1    | 0.06 MB  | ~0 MB    | 1 MB        |
| satellite_ssta         | 2000×3900 | 1    | 62 MB    | 18 MB    | 540 MB      |
| **4 current products** |           |      |          |          | **~586 MB** |
| **10 products (est.)** |           |      |          |          | **~1.5 GB** |

The satellite_ssta product alone consumes 540 MB for 30 dates at 18 MB/date (lz4). Redis is a pure in-memory store — it cannot spill to disk. Practical Redis deployments run 512 MB–4 GB (beyond that, cost grows quickly). With Redis internal overhead (~30%), a 1 GB Redis instance can barely fit the 4 current products at 30 days — and has no headroom for adding more products or longer windows.

**Why Redis was ruled out:**

1. **Memory ceiling.** Redis must hold everything in RAM. The satellite SSTA product alone (62 MB/date raw, 18 MB lz4) makes the per-product memory cost expensive. Caching 30 dates for 4 products requires ~760 MB of Redis memory including overhead — already pushing a 1 GB instance. With 10 products the requirement is ~2 GB, demanding an ElastiCache instance that costs more than the EC2 running the app.

2. **Cost at scale.** ElastiCache `cache.r6g.large` (6.38 GB) costs ~$120/month. A 20 GB EBS volume costs ~$2/month. For large scientific grids, disk is ~60× cheaper per GB.

3. **Operational complexity.** A Redis service (Docker Compose or ElastiCache) is an additional infrastructure dependency. Failure modes (OOM crash, network partition) require handling and monitoring. Disk has no comparable failure surface.

4. **Instance sharing is not required.** IMOS ocean data is the same for every user and every instance — it is not user-session-specific. There is no benefit to a shared Redis cluster: each instance can independently cache identical data. The only scenario where sharing matters is a scale-out event (new instance cold start), which at demo and small production scale is rare and tolerable (~60s prewarm with parallel workers).

---

### Option 2 — EFS (Elastic File System)

AWS EFS is a managed network filesystem that can be mounted into both ECS Fargate and EC2 tasks. It is the only shared-disk option for Fargate deployments.

**Why EFS was ruled out:**

1. **Read latency.** EFS is a network filesystem. Read latency is typically 5–50ms per operation, compared to ~5ms for local NVMe disk. For compressed slice files (~10–20 MB each), the actual bottleneck is network throughput, not seek time — but under concurrent load, EFS throughput can saturate, pushing reads toward the high end. The disk cache's value proposition is serving slices at ~30ms (read + decompress); EFS makes that less reliable.

2. **Cost.** EFS charges $0.30/GB-month for standard storage plus $0.01/GB for reads. For 20 GB of cached data with frequent reads, the monthly cost is material and grows with traffic. An EBS volume at $0.10/GB-month with no per-read charge is 3–5× cheaper under normal traffic patterns.

3. **Complexity for our scale.** EFS requires VPC configuration, security groups, and mount targets in each AZ. For a single-instance demo/small-production deployment, this overhead is not justified.

---

### Option 3 — EBS Persistent Disk (Selected)

Slices are serialised with `lz4.frame.compress(pickle.dumps(ds))` and written to an EBS (Elastic Block Store) volume attached to the EC2 host. The container bind-mounts the host directory so the cache persists across container restarts and redeploys. EBS is network-attached block storage that lives independently of the instance or container — data survives container rebuilds, image updates, and instance reboots.

**Why EBS disk was chosen:**

1. **Capacity is not a constraint.** A 20 GB EBS gp3 volume holds months of data for all current products, with the satellite SSTA (the largest at 18 MB/date lz4) consuming only ~540 MB for 30 dates. Adding more products or longer windows requires increasing `DISK_CACHE_LIMIT_GB` and optionally resizing EBS — both trivial operations.

2. **Read performance is sufficient.** Local disk read + lz4 decompress takes ~30ms for a typical slice. This is ~60× faster than a cold S3 read (~2s) and well within the acceptable response time for a warm request. Redis would be ~5ms faster (network round-trip vs disk seek), but that difference is imperceptible to a map user and not worth the cost and complexity trade-off.

3. **Per-instance caching is correct for this workload.** The data served is identical for every request to every instance — ocean grid data for a given product and date does not vary per user. Each instance independently warms the same cache. On scale-out, a new instance runs parallel prewarm (~60s for 4 products × 30 dates with 4 workers) and is then fully warm. At current scale, scale-out events are rare.

4. **Persistence model matches deployment.** The disk cache directory is bind-mounted from the host (`./slice_cache:/app/slice_cache` in Docker Compose; equivalent bind mount in ECS EC2 task definition). It survives container restarts, redeploys, and image rebuilds. On a warm restart, the prewarm skips already-cached dates and completes in under a second.

5. **No additional infrastructure.** No Redis service, no ElastiCache cluster, no EFS mount target. The cache is the host's own EBS root volume (already present). Operational surface area stays minimal.

6. **ECS EC2 launch type is a natural fit.** ECS EC2 gives you owned EC2 instances registered with an ECS cluster. The containers run with exactly the same bind-mount capability as Docker Compose on a plain EC2 instance. The environment variable (`DISK_CACHE_PATH`) and the mount path stay identical — no code changes between Docker Compose and ECS EC2 deployment.

---

### Option 4 — Ephemeral Disk (Instance Store / Fargate Ephemeral Storage)

Both EC2 instance store and ECS Fargate ephemeral storage provide fast local disk that is tied to the instance or task lifecycle — the data is lost when the container stops. Despite the lack of persistence, this is still a viable option for this workload.

**EC2 instance store** is physically attached NVMe storage available on certain instance families (e.g. `m5d`, `r5d`, `c5d`). It is faster than EBS (~0.1ms vs ~1ms seek) but lost when the instance stops or is terminated.

**ECS Fargate ephemeral storage** is SSD-backed local storage automatically allocated per Fargate task — 20 GB by default, configurable up to 200 GB. It requires no configuration, no bind mounts, and no extra cost. It is wiped when the task stops.

**Why non-persistence is acceptable:**

The prewarm runs on every startup and rebuilds the full cache from S3. With `PREWARM_WORKERS=4`, 4 products × 30 dates takes ~60s. After that the instance is fully warm. Container restarts are infrequent events — once warm, the server runs continuously for days or weeks. The 60s cold window on restart is acceptable for this workload.

This makes ephemeral disk the right choice for **ECS Fargate** deployments: it avoids EFS (costly, slower) and ElastiCache Redis (expensive, memory-limited) while still providing disk-speed cache during normal operation. No extra infrastructure required.

**Implementation:** identical to Option 3 — set `DISK_CACHE_PATH` to the ephemeral path (e.g. `/tmp/slice_cache` for Fargate, or the instance store mount point on EC2). The only difference is no host bind mount in docker-compose; the path lives inside the container or on the ephemeral volume.

---

## Decision Summary

|                               | Redis                               | EFS                        | Ephemeral Disk            | EBS Persistent Disk (chosen) |
| ----------------------------- | ----------------------------------- | -------------------------- | ------------------------- | ---------------------------- |
| Capacity                      | 512 MB–4 GB (expensive)             | Effectively unlimited      | 20–200 GB (Fargate task)  | 10s–100s GB (cheap)          |
| Read latency                  | ~1ms + ~25ms decompress             | ~5–50ms + ~25ms decompress | ~0.1–1ms + ~25ms decompress | ~1ms + ~25ms decompress     |
| Satellite 30 days (18 MB lz4) | 540 MB — hits limit                 | Fine                       | Fine                      | Fine                         |
| 10 products 30 days           | ~1.5 GB — infeasible                | Fine                       | Fine                      | ~1.5 GB — trivial            |
| Shared across instances       | Yes                                 | Yes                        | No                        | No                           |
| Survives container restart    | Yes                                 | Yes                        | No — prewarm on restart   | Yes (bind-mounted EBS)       |
| New instance cold window      | Instant (shared)                    | Instant (shared)           | ~60s (prewarm from S3)    | ~60s first time, <1s after   |
| ECS Fargate compatible        | Yes (ElastiCache)                   | Yes                        | Yes (ephemeral storage)   | No                           |
| ECS EC2 compatible            | Yes                                 | Yes                        | Yes (instance store)      | Yes                          |
| Extra infrastructure          | Redis container / ElastiCache       | EFS volume + mount targets | None                      | None (uses existing EBS)     |
| Approx. cost for 20 GB        | ~$120/month (ElastiCache r6g.large) | ~$6/month + read charges   | Included in instance/task | ~$2/month (EBS gp3)          |

**Chosen: EBS persistent disk.** Current deployment is EC2 + Docker Compose, where EBS is already present as the root volume. Persistence means warm restarts complete in under a second (no re-fetch from S3). For a future Fargate migration, ephemeral disk is the preferred alternative — same code, no extra infrastructure, ~60s cold window on restart is acceptable.

---

## Cache Architecture (Implemented)

```
Request
  │
  ├─ L2 hit? (_slice_cache, in-memory LRU)   → <1ms
  │
  ├─ L3 hit? (disk, lz4 pkl)                 → ~30ms  → populate L2
  │
  └─ S3 miss (.compute())                    → ~2s    → populate L2, write L3
                   ▲
     Written only by startup prewarm / 4-hour refresh cron
```

**Why L2 is still needed alongside disk:**
`_processed_cache` in `data_renderer.py` is keyed by `(id(ds), lod)`. This requires `_slice_cache` to return the **same Python object** for repeated calls within a process — each disk deserialisation produces a new object with a different `id`, so the processed grid cache would never hit without L2. L2 also avoids redundant disk reads for the 10–20 tile requests that arrive for the same product+date in a single map session.

**Disk eviction strategy:**
Files are evicted sorted by `(size ascending, date ascending)` — small-grid products (cheap to re-fetch from S3) are evicted before large-grid products (expensive ~2s re-fetch), and within each size group, oldest dates go first. This keeps the satellite SSTA cache (most valuable) resident the longest under disk pressure.

---

## Upgrade Path

| Phase                                   | Deployment           | L3 cache                                                            |
| --------------------------------------- | -------------------- | ------------------------------------------------------------------- |
| Current                                 | EC2 + Docker Compose | EBS persistent disk (`DISK_CACHE_PATH=./slice_cache`)               |
| Next                                    | ECS EC2 launch type  | EBS persistent disk (host bind mount, same env var, no code change) |
| Future (Fargate, no shared state needed)| ECS Fargate          | Ephemeral disk (`DISK_CACHE_PATH=/tmp/slice_cache`, ~60s on restart)|
| Future (Fargate, shared across tasks)   | ECS Fargate          | ElastiCache Redis (`REDIS_URL`)                                     |

All three disk options use the same `DISK_CACHE_PATH` env var and identical code in `loader.py`. Moving between them requires only changing the env var and mount configuration — no application code changes. Redis requires adding `REDIS_URL` and is only warranted if shared cache across Fargate tasks becomes a hard requirement.
