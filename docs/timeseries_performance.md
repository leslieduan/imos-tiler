# Timeseries Endpoint Performance

## Problem

`GET /{data_tiles,visual_tiles}/{product_id}/timeseries?lat=…&lon=…&from=…&to=…`
([`routers/public/products.py:169`](../src/app/routers/public/products.py)) is slow for
long date ranges:

| Request                                                         | Wall time |
| --------------------------------------------------------------- | --------- |
| `sea_level_anomaly`, 2020-01-01 → 2026-04-24                    | ~22 s     |
| `satellite_austemp_heatwave_8day_ssta`, 2012-01-01 → 2026-04-20 | >60 s     |

The intuition that "dask reads chunks in parallel, and same-region S3 has high
bandwidth, so fetching shouldn't be the bottleneck" is **only half right**.
Parallelism removes _latency_ as the bottleneck. It does nothing about _volume_ —
and this endpoint's access pattern forces it to move gigabytes off S3 to return
a few kilobytes.

---

## Root cause: chunk-level read amplification

The work happens in [`load_point_series`](../src/app/services/caching/slice_cache.py)
(`slice_cache.py:112`):

```python
point = store[variables].sel(lat=lat, lon=lon, method="nearest")
...
point = point.sel(time=timestamps).compute()
```

`.sel(lat, lon, method="nearest")` narrows the request to a **single grid cell**.
But Zarr/dask can only read at **chunk granularity** — it cannot fetch a
sub-region of a chunk. To extract one pixel at one timestep, it must download and
decompress the _entire spatial slab_ of the chunk that contains that pixel.

The stores are chunked **fat in space, thin in time** (see [`dataset.md`](dataset.md)),
which is optimal for the tile hot path (one date, full spatial grid) and _pessimal_
for the timeseries pattern (one point, all dates):

### GSLA — `sea_level_anomaly`

- Chunk shape `(5, 351, 641)` float64. The spatial dims are **one chunk each**.
- One time-chunk read = `5 × 351 × 641 × 8 B` ≈ **9 MB**, to extract 5 wanted
  values (40 bytes).
- Range spans the bulk of the store's 2338 timesteps → ~450 time-chunks
  (one chunk per 5 steps).
- **Total transferred ≈ 450 × 9 MB ≈ 4 GB** to return ~18 KB of data.

### Satellite — `satellite_austemp_heatwave_8day_ssta`

- Chunk shape `(5, 1000, 1300)` float64.
- One time-chunk read = `5 × 1000 × 1300 × 8 B` ≈ **52 MB**.
- The 5225 timesteps span ~2012→2026 (roughly daily), so the range selects
  almost the whole record → ~1000 time-chunks.
- **Total transferred ≈ 1000 × 52 MB ≈ 50 GB** to return ~40 KB of data.

| Product             | Bytes / time-chunk | Time-chunks in range | Transferred | Useful data | Amplification |
| ------------------- | ------------------ | -------------------- | ----------- | ----------- | ------------- |
| `sea_level_anomaly` | ~9 MB              | ~450                 | ~4 GB       | ~18 KB      | ~2×10⁵        |
| `satellite_…_ssta`  | ~52 MB             | ~1000                | ~50 GB      | ~40 KB      | ~1×10⁶        |

The 22 s vs 60 s+ gap is explained almost entirely by the per-time-chunk size
ratio (52 / 9 ≈ **5.8×** more bytes per step) times the larger number of steps.

---

## Why parallelism and same-region S3 don't rescue it

- **The workload is bandwidth- and CPU-bound, not latency-bound.** Parallel
  range-GETs hide round-trip latency when fetching many _small_ objects. Here every
  object is large and _all_ of them are needed; ~50 GB across the wire is ~50 GB no
  matter how many sockets carry it. At a generous ~800 MB/s aggregate that alone is
  ~60 s.
- **Decompression is real CPU work** competing on a bounded dask thread pool
  (and partly serialised by the GIL, depending on codec). Inflating tens of GB of
  float64 is not free on an EC2 box with a handful of vCPUs.
- **This path bypasses the L2/L3 caches by design** — see the `load_point_series`
  docstring (`slice_cache.py:131`). Every request re-pays the full cost; there is no
  warm second hit.

The store is opened with `chunks={}` (native on-disk chunking — see
[`registry.py:66`](../src/app/services/store/registry.py) and the comment there).
That is the correct choice for tile serving and must not change. It is simply
mismatched to the timeseries access pattern.

---

## What helps

The only two levers on the read path are **bytes per timestep** (the store's chunk
shape, which we don't own — these are public IMOS/AODN buckets) and **number of
timesteps**.

1. **Build a timeseries-optimised derived store** — a transposed/rechunked copy
   (`time` contiguous, small `lat`/`lon` chunks) written once and read cheaply
   forever. This is the proper fix: it turns 50 GB into megabytes. Cost is a
   data-engineering job plus extra storage.
2. **Cap or downsample the range** — coarser cadence for multi-year spans, or
   pagination. A 14-year daily series in a chart does not need every point.

Option 2 is a server-side change that lives alongside the existing caching
strategy ([`technical.md` §10](technical.md#10-caching-strategy)). Option 1 is an
upstream/offline pipeline change.

---

## TL;DR

The stores are chunked for **single-date, full-grid** reads (tiles). The timeseries
endpoint asks for the orthogonal pattern — **single-point, all-dates** — which forces
Zarr to read a whole spatial slab per time-chunk just to pluck one pixel. That is a
10⁵–10⁶× read amplification. Same-region S3 and dask parallelism remove latency, but
the fundamental cost is _volume_, and you can't parallelize your way out of moving
tens of gigabytes.
