# Performance Benchmarks

## Methodology

All times are **end-to-end response times measured at the client**, covering the full round-trip:
server processing (S3 fetch → render → encode) + network transfer from server to client.

### Environments

| Environment | Machine | S3 connectivity |
|---|---|---|
| Local | MacBook (macOS) | Internet — Sydney → S3 ap-southeast-2 |
| AWS EC2 | t3.micro, ap-southeast-2 | AWS internal network |

### Cold vs hot

| Term | Definition |
|---|---|
| **Cold** | First request for a product+date. The Zarr store opens, the time slice is fetched from S3, rendered, and cached in memory. |
| **Hot** | Any subsequent request for the **same product+date**, regardless of tile coordinates (z/x/y). Once any tile for a given product+date is fetched, the full date slice is in memory — all other tile coordinates for that product+date are served with no S3 I/O. |

> **Note:** Manifest hot times are not shown. A manifest is a single fixed URL per product+date, typically fetched once to initialise the client. Unlike tiles — where many z/x/y combinations benefit from the same cached slice — there is only one manifest URL to request, so a hot measurement is not meaningful.

---

## Results

### satellite_austemp_heatwave_8day_ssta

| | |
|---|---|
| **Store** | `s3://aodn-cloud-optimised/satellite_austemp_heatwave_8day.zarr` |

| Dimension | Size | Chunk | Spatial chunks needed |
|---|---|---|---|
| time | 5225 | 5 | — |
| lat | 2000 | 1000 | 2 |
| lon | 3900 | 1300 | 3 |

**Time-slice size:** ~62 MB — 6 S3 reads (2 lat chunks × 3 lon chunks, each 1000 × 1300 × float64)

**Manifest** (cold only)

| | Local | AWS EC2 |
|---|---|---|
| Cold | 26.07 s | 759 ms |

**Tiles**

| | Local | AWS EC2 |
|---|---|---|
| Cold | 20.42 s | 1.2 s |
| Hot | 5 ms | 200 ms |

> Largest dataset. Covering the full 2000 × 3900 grid requires 6 S3 reads (2 lat chunks × 3 lon chunks), dominating cold-start time.

---

### sea_level_anomaly

| | |
|---|---|
| **Store** | `s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/` |

| Dimension | Size | Chunk | Spatial chunks needed |
|---|---|---|---|
| time | 2338 | 5 | — |
| lat | 351 | 351 | 1 |
| lon | 641 | 641 | 1 |

**Time-slice size:** ~1.7 MB per variable — 1 S3 read (full spatial slab in a single chunk)

**Manifest** (cold only)

| | Local | AWS EC2 |
|---|---|---|
| Cold | 4.62 s | 291 ms |

**Tiles**

| | Local | AWS EC2 |
|---|---|---|
| Cold | 4.74 s | 377 ms |
| Hot | 5 ms | 195 ms |

---

### ocean_current

| | |
|---|---|
| **Store** | `s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/` (shared with `sea_level_anomaly`) |

| Dimension | Size | Chunk | Spatial chunks needed |
|---|---|---|---|
| time | 2338 | 5 | — |
| lat | 351 | 351 | 1 |
| lon | 641 | 641 | 1 |

**Time-slice size:** ~3.4 MB — 2 S3 reads (UCUR + VCUR, one full spatial slab each)

**Manifest** (cold only)

| | Local | AWS EC2 |
|---|---|---|
| Cold | 10.68 s | 547 ms |

**Tiles**

| | Local | AWS EC2 |
|---|---|---|
| Cold | 9.19 s | 587 ms |
| Hot | 5 ms | 152 ms |

> Shares the same Zarr store as `sea_level_anomaly` but reads two variables (UCUR + VCUR), roughly doubling the S3 I/O.

---

### radar_SouthAustraliaGulfs_wind_delayed_qc_wdir

| | |
|---|---|
| **Store** | `s3://aodn-cloud-optimised/radar_SouthAustraliaGulfs_wind_delayed_qc.zarr` |

| Dimension | Size | Chunk | Spatial chunks needed |
|---|---|---|---|
| time | 38129 | 100 | — |
| lat | 74 | 74 | 1 |
| lon | 102 | 102 | 1 |

**Time-slice size:** ~5.9 MB — 1 S3 read (full spatial grid in a single chunk; chunk also spans 100 time steps)

**Manifest** (cold only)

| | Local | AWS EC2 |
|---|---|---|
| Cold | 376 ms | 205 ms |

**Tiles**

| | Local | AWS EC2 |
|---|---|---|
| Cold | 380 ms | 190 ms |
| Hot | 5 ms | 83 ms |

> Smallest spatial grid. The full domain fits in a single tile (`lod_grids = {1: (1, 1)}`), giving the fastest cold-start of all products.

---

## Key observations

- **EC2 cold start is 10–35× faster than local** — S3 and EC2 are on the same AWS internal network. Local cold requests pay internet latency for every S3 chunk read.
- **Local hot (5 ms)** — client and server are on the same machine. Response time is purely in-memory: cache lookup + PNG encode, no network transfer.
- **AWS hot (83–200 ms)** — S3 I/O is eliminated by the cache, but the tile PNG still travels from EC2 to the client over the internet. Variation reflects response payload size.
- **Chunk design drives cold-start time** — `satellite_austemp` requires 6 S3 reads per slice; `sea_level_anomaly`, `ocean_current`, and `radar` pack the full spatial grid into a single chunk, so one read is enough.
