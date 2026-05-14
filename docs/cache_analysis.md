# Cache Design Analysis

## Problem

Cold tile requests for large dadaset take up to ~2s because `load_slice` reads Zarr chunks from S3. The in-memory `_slice_cache` eliminates repeat hits within one process lifetime, but is wiped on every container restart. CloudFront reduces origin load but has its own TTL and eviction — tiles regularly cycle out of the CDN cache and those misses flow back to the origin, hitting the cold S3 path again. The cold path therefore occurs both on container restart and on any CloudFront eviction.

The goal is a persistent L2 cache that survives restarts and reduces cold S3 reads to milliseconds.

---

## Options Evaluated

### Option 1 — Redis

Redis is an in-memory key-value store, commonly used as a distributed cache. The core appeal is sub-millisecond reads and sharing across multiple instances.

**How it would work:**

- Slices serialised with `lz4.frame.compress(pickle.dumps(ds))` and stored as Redis string keys
- Populated by startup prewarm and a background refresh cron only — not by request hits
- On L1 miss, probe Redis before going to S3

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

### Option 3 — Local Disk Cache (Selected)

Slices are serialised with `lz4.frame.compress(pickle.dumps(ds))` and written to the host filesystem. The container bind-mounts the host directory so the cache persists across container restarts and redeploys.

**Why disk was chosen:**

1. **Capacity is not a constraint.** A 20 GB EBS volume holds months of data for all current products, with the satellite SSTA (the largest at 18 MB/date lz4) consuming only ~540 MB for 30 dates. Adding more products or longer windows requires increasing `DISK_CACHE_LIMIT_GB` and optionally resizing EBS — both trivial operations.

2. **Read performance is sufficient.** Local disk read + lz4 decompress takes ~30ms for a typical slice. This is ~60× faster than a cold S3 read (~2s) and well within the acceptable response time for a warm request. Redis would be ~5ms faster (network round-trip vs disk seek), but that difference is imperceptible to a map user and not worth the cost and complexity trade-off.

3. **Per-instance caching is correct for this workload.** The data served is identical for every request to every instance — ocean grid data for a given product and date does not vary per user. Each instance independently warms the same cache. On scale-out, a new instance runs parallel prewarm (~60s for 4 products × 30 dates with 4 workers) and is then fully warm. At current scale, scale-out events are rare.

4. **Persistence model matches deployment.** The disk cache directory is bind-mounted from the host (`./slice_cache:/app/slice_cache` in Docker Compose; equivalent bind mount in ECS EC2 task definition). It survives container restarts, redeploys, and image rebuilds. The data stays on the EC2 host's EBS volume until the instance is terminated — which on EC2 and ECS EC2 launch type is a long-lived event, not routine.

5. **No additional infrastructure.** No Redis service, no ElastiCache cluster, no EFS mount target. The cache is the host's own disk, which already exists. Operational surface area stays minimal.

6. **ECS EC2 launch type is a natural fit.** ECS EC2 gives you owned EC2 instances registered with an ECS cluster. The containers run with exactly the same bind-mount capability as Docker Compose on a plain EC2 instance. The environment variable (`DISK_CACHE_PATH`) and the mount path stay identical — no code changes between Docker Compose and ECS EC2 deployment.

---

## Decision Summary

|                               | Redis                               | EFS                        | Local Disk (chosen)     |
| ----------------------------- | ----------------------------------- | -------------------------- | ----------------------- |
| Capacity                      | 512 MB–4 GB (expensive)             | Effectively unlimited      | 10s–100s GB (cheap)     |
| Read latency                  | ~1ms + ~25ms decompress             | ~5–50ms + ~25ms decompress | ~5ms + ~25ms decompress |
| Satellite 30 days (18 MB lz4) | 540 MB — hits limit                 | Fine                       | Fine                    |
| 10 products 30 days           | ~1.5 GB — infeasible                | Fine                       | ~1.5 GB — trivial       |
| Shared across instances       | Yes                                 | Yes                        | No                      |
| New instance cold window      | Instant (shared)                    | Instant (shared)           | ~60s (parallel prewarm) |
| Survives container restart    | Yes                                 | Yes                        | Yes (bind-mounted host) |
| ECS Fargate compatible        | Yes (ElastiCache)                   | Yes                        | No                      |
| ECS EC2 compatible            | Yes                                 | Yes                        | Yes                     |
| Extra infrastructure          | Redis container / ElastiCache       | EFS volume + mount targets | None                    |
| Approx. cost for 20 GB        | ~$120/month (ElastiCache r6g.large) | ~$6/month + read charges   | ~$2/month (EBS gp3)     |

**Chosen: local disk.** The workload is read-heavy, the data is identical across instances, and the dataset is too large for practical Redis deployment. Disk delivers Redis-comparable read latency at a fraction of the cost and with zero additional infrastructure.

---

## Cache Architecture (Implemented)

```
Request
  │
  ├─ L1 hit? (_slice_cache, in-memory LRU)   → <1ms
  │
  ├─ L2 hit? (disk, lz4 pkl)                 → ~30ms  → populate L1
  │
  └─ L3 miss (S3 .compute())                 → ~2s    → populate L1, write L2
                   ▲
     Written only by startup prewarm / 4-hour refresh cron
```

**Why L1 is still needed alongside disk:**
`_processed_cache` in `data_renderer.py` is keyed by `(id(ds), lod)`. This requires `_slice_cache` to return the **same Python object** for repeated calls within a process — each disk deserialisation produces a new object with a different `id`, so the processed grid cache would never hit without L1. L1 also avoids redundant disk reads for the 10–20 tile requests that arrive for the same product+date in a single map session.

**Disk eviction strategy:**
Files are evicted sorted by `(size ascending, date ascending)` — small-grid products (cheap to re-fetch from S3) are evicted before large-grid products (expensive ~2s re-fetch), and within each size group, oldest dates go first. This keeps the satellite SSTA cache (most valuable) resident the longest under disk pressure.

---

## Upgrade Path

| Phase                                   | Deployment           | L2 cache                                               |
| --------------------------------------- | -------------------- | ------------------------------------------------------ |
| Current                                 | EC2 + Docker Compose | Local disk (`DISK_CACHE_PATH`)                         |
| Next                                    | ECS EC2 launch type  | Local disk (bind mount, same env var, no code changes) |
| Future (if multi-AZ scale-out required) | ECS Fargate          | ElastiCache Redis (`REDIS_URL`)                        |

Switching to Redis if needed requires only adding `REDIS_URL` to the environment. The disk and Redis paths are independent — both can be implemented in `loader.py` and selected by which env var is set. No logic changes to the rest of the application.
