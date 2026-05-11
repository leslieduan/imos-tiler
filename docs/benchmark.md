# Performance Benchmarks

## Methodology

All times are **end-to-end response times measured at the client**, covering the full round-trip:
server processing (S3 fetch → render → encode) + network transfer from server to client.

### Environment

| Environment | Machine | S3 connectivity |
|---|---|---|
| AWS EC2 | t3.medium, ap-southeast-2 | AWS internal network |

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

| | AWS EC2 |
|---|---|
| Cold | 759 ms |

**Tiles**

| | AWS EC2 |
|---|---|
| Cold | 1.2 s |
| Hot | 200 ms |

**Point**

| | AWS EC2 |
|---|---|
| Cold | 767 ms |
| Hot | 74 ms |

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

| | AWS EC2 |
|---|---|
| Cold | 291 ms |

**Tiles**

| | AWS EC2 |
|---|---|
| Cold | 377 ms |
| Hot | 195 ms |

**Point**

| | AWS EC2 |
|---|---|
| Cold | 314 ms |
| Hot | 71 ms |

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

| | AWS EC2 |
|---|---|
| Cold | 547 ms |

**Tiles**

| | AWS EC2 |
|---|---|
| Cold | 587 ms |
| Hot | 152 ms |

**Point**

| | AWS EC2 |
|---|---|
| Cold | 400 ms |
| Hot | 73 ms |

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

| | AWS EC2 |
|---|---|
| Cold | 205 ms |

**Tiles**

| | AWS EC2 |
|---|---|
| Cold | 190 ms |
| Hot | 83 ms |

**Point**

| | AWS EC2 |
|---|---|
| Cold | 200 ms |
| Hot | 73 ms |

> Smallest spatial grid. The full domain fits in a single tile (`lod_grids = {1: (1, 1)}`), giving the fastest cold-start of all products.

---

## Key observations

- **AWS hot tiles (83–200 ms)** — S3 I/O is eliminated by the cache, but the tile PNG still travels from EC2 to the client over the internet. Variation reflects response payload size.
- **AWS hot point (71–74 ms)** — consistently fast across all products once the slice is cached, since the response is a small JSON value with no PNG encoding or payload size variation.
- **Chunk design drives cold-start time** — `satellite_austemp` requires 6 S3 reads per slice; `sea_level_anomaly`, `ocean_current`, and `radar` pack the full spatial grid into a single chunk, so one read is enough.
