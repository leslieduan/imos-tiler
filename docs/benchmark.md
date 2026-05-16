# Performance Benchmarks

> **Scope.** The figures in this document are measured against a handful of example Zarr stores that have been used during development. **They are not benchmarks of any particular production deployment** — a production deployment registers products at runtime via the admin API (see [`technical.md` §13](technical.md#13-adding-a-new-product)), and its actual product mix may differ.
>
> The example stores below were chosen to span the **size classes** the server is expected to handle: one **satellite-class** product (large grid, multi-chunk per slice, dominates RAM/disk in production) and two **GSLA-class** products (small grid, one chunk per slice). Numbers for a new product can be estimated by comparing its grid size and chunk layout against the closest size class here.

## Methodology

All times are **end-to-end response times measured at the client**, covering the full round-trip:
server processing (disk/S3 fetch → render → encode) + network transfer from server to client.

### Environment

| Environment | Machine                  | S3 connectivity      |
| ----------- | ------------------------ | -------------------- |
| AWS EC2     | t3.large, ap-southeast-2 | AWS internal network |

### Cache tiers

The server has a three-tier cache. Each request is served from the fastest available tier:

| Term          | Definition                                                                                                                                                                          |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Hot**       | Slice is in the in-memory LRU cache. No S3 or disk I/O — chunk extraction and PNG encoding only.                                                                                    |
| **Disk warm** | Slice is not in memory but is present on disk (all dates within the last 30 days are prewarmed to disk on startup and refreshed every 4 hours). Reads from local EBS instead of S3. |
| **Cold**      | Slice is not in memory or on disk. The full time slice is fetched from S3, rendered, and written to both the disk cache and memory cache.                                           |

> **Note:** Manifest hot times are not shown. A manifest is a single fixed URL per product+date, typically fetched once to initialise the client. Unlike tiles — where many z/x/y combinations benefit from the same cached slice — there is only one manifest URL to request, so a hot measurement is not meaningful. Disk warm is shown since a manifest request after a server restart (cache cleared, disk still populated) is a realistic scenario.

---

## Results

### satellite_austemp_heatwave_8day_ssta

|           |                                                                  |
| --------- | ---------------------------------------------------------------- |
| **Store** | `s3://aodn-cloud-optimised/satellite_austemp_heatwave_8day.zarr` |

| Dimension | Size | Chunk | Spatial chunks needed |
| --------- | ---- | ----- | --------------------- |
| time      | 5225 | 5     | —                     |
| lat       | 2000 | 1000  | 2                     |
| lon       | 3900 | 1300  | 3                     |

**Time-slice size:** ~62 MB — 6 S3 reads (2 lat chunks × 3 lon chunks, each 1000 × 1300 × float64)

**Manifest**

|           | AWS EC2 |
| --------- | ------- |
| Cold      | 759 ms  |
| Disk warm | 380 ms  |

**Tiles**

|           | AWS EC2 |
| --------- | ------- |
| Cold      | 1.2 s   |
| Disk warm | 500 ms  |
| Hot       | 200 ms  |

**Point**

|           | AWS EC2 |
| --------- | ------- |
| Cold      | 767 ms  |
| Disk warm | 385 ms  |
| Hot       | 74 ms   |

> Largest dataset. Covering the full 2000 × 3900 grid requires 6 S3 reads (2 lat chunks × 3 lon chunks), dominating cold-start time.

---

### sea_level_anomaly

|           |                                                                            |
| --------- | -------------------------------------------------------------------------- |
| **Store** | `s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/` |

| Dimension | Size | Chunk | Spatial chunks needed |
| --------- | ---- | ----- | --------------------- |
| time      | 2338 | 5     | —                     |
| lat       | 351  | 351   | 1                     |
| lon       | 641  | 641   | 1                     |

**Time-slice size:** ~1.7 MB per variable — 1 S3 read (full spatial slab in a single chunk)

**Manifest**

|           | AWS EC2 |
| --------- | ------- |
| Cold      | 291 ms  |
| Disk warm | 145 ms  |

**Tiles**

|           | AWS EC2 |
| --------- | ------- |
| Cold      | 377 ms  |
| Disk warm | 210 ms  |
| Hot       | 195 ms  |

**Point**

|           | AWS EC2 |
| --------- | ------- |
| Cold      | 314 ms  |
| Disk warm | 155 ms  |
| Hot       | 71 ms   |

---

### ocean_current

|           |                                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------------ |
| **Store** | `s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/` (shared with `sea_level_anomaly`) |

| Dimension | Size | Chunk | Spatial chunks needed |
| --------- | ---- | ----- | --------------------- |
| time      | 2338 | 5     | —                     |
| lat       | 351  | 351   | 1                     |
| lon       | 641  | 641   | 1                     |

**Time-slice size:** ~3.4 MB — 2 S3 reads (UCUR + VCUR, one full spatial slab each)

**Manifest**

|           | AWS EC2 |
| --------- | ------- |
| Cold      | 547 ms  |
| Disk warm | 275 ms  |

**Tiles**

|           | AWS EC2 |
| --------- | ------- |
| Cold      | 587 ms  |
| Disk warm | 295 ms  |
| Hot       | 152 ms  |

**Point**

|           | AWS EC2 |
| --------- | ------- |
| Cold      | 400 ms  |
| Disk warm | 200 ms  |
| Hot       | 73 ms   |

> Shares the same Zarr store as `sea_level_anomaly` but reads two variables (UCUR + VCUR), roughly doubling the S3 I/O.

---

## Key observations

- **Hot tiles (150–200 ms)** — S3 and disk I/O are both eliminated by the in-memory cache. Variation reflects PNG payload size.
- **Hot point (71–74 ms)** — consistently fast across all products; small JSON response with no PNG encoding.
- **Disk warm (155–500 ms)** — all dates within `CACHE_DAYS` (default 30) are prewarmed to disk on startup and refreshed every 4 hours. A request that misses the in-memory LRU (e.g. after a restart or cache eviction) reads from local EBS rather than S3, roughly halving cold latency and capped at ~500 ms even for the satellite-class slice.
- **Chunk design drives S3 cold-start time** — `satellite_austemp` requires 6 S3 reads per slice (2 lat chunks × 3 lon chunks); `sea_level_anomaly` and `ocean_current` pack the full spatial grid into a single chunk, so one read is enough. In practice, S3 cold requests only occur for dates older than `CACHE_DAYS` or before startup prewarm completes.
- **Estimating a new product** — a product whose grid is similar in size to `sea_level_anomaly` (351 × 641) but with `K` variables will land near `K × sea_level_anomaly` cold-time (UCUR + VCUR is the worked example). A new satellite-class product (~2000 × 3900) sized for `M` lat-chunks × `N` lon-chunks will scale cold time as `(M × N) / 6` relative to the satellite figures above.
