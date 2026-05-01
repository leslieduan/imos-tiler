# NetCDF vs Zarr for Cloud-Based Tile Serving

## NetCDF3 vs NetCDF4

### NetCDF3
NetCDF3 stores all metadata (variable names, dimensions, attributes) in a **single header at the start of the file**, followed by raw data in a fixed layout. Opening a file requires one read at offset 0 — the entire structure is known immediately.

```
[ HEADER | VAR_1 DATA | VAR_2 DATA | ... ]
  ↑
  one read, everything known
```

Limitations:
- No compression — data is stored as raw bytes
- No chunking — reading one time step from a large file requires scanning sequentially
- 2 GB variable size limit

### NetCDF4 (built on HDF5)
NetCDF4 uses HDF5 as its storage format, which is essentially a **mini filesystem inside a file**. It adds compression, chunking, and support for large datasets. However, its internal structure is a B-tree where metadata nodes — variable headers, chunk index entries, data pointers — are allocated dynamically at write time and can end up **anywhere in the file**.

```
offset 0:         superblock
offset 800:       root group
offset 45,000:    variable header (sst_anom_mosaic)
offset 2,100,000: B-tree node
offset 67,000,000: B-tree node
offset 98,000,000: chunk index
...
```

Opening a file means traversing this tree, following pointers to scattered locations throughout the file.

**On local disk** this is fine — a disk seek takes ~0.01ms, so 70+ scattered reads take < 1ms total.

**On S3 via s3fs** each scattered read that misses the block cache becomes a separate HTTP request:
- Home internet: 70+ requests × ~200ms latency = **~16 seconds**
- AWS in-region (same S3 region): 70+ requests × ~1ms latency = **~70ms**

The data itself is not the bottleneck — the **number of round-trips** is.

---

## Why Cloud Storage Changes Everything

Local disk and cloud object storage have fundamentally different performance characteristics:

| | Local Disk | S3 (home internet) | S3 (AWS in-region) |
|---|---|---|---|
| Seek / round-trip latency | ~0.01ms | ~200ms | ~1ms |
| Bandwidth | ~500 MB/s | ~3 MB/s | ~1 GB/s |
| Cost per read | negligible | 20,000× disk | 100× disk |

HDF5/NetCDF4 was designed in the 1990s–2000s for local disk and NFS storage. Cloud object storage did not exist. A design that makes 70+ seeks is fine on disk and catastrophic over HTTP.

---

## Observed Performance (this project, home internet)

For `austemp_sst_anomaly_sst_anom_mosaic` — a 101 MB compressed NetCDF4 file on S3:

```
s3.ls:          0.27s   (list year directory)
open_dataset:  16.66s   (HDF5 metadata traversal — ~70+ HTTP round-trips)
variable read: ~15s     (20 MB uncompressed variable data transferred)
total:         ~32s     for first tile/manifest request
```

The LRU cache means this cost is paid **once per product per date per server session**. Subsequent requests for the same product+date are instant.

At AWS ap-southeast-2 (co-located with S3), the same request would take ~1–2s.

---

## Zarr

Zarr was designed in ~2015 specifically to solve the cloud access problem. It replaces the monolithic file with a **directory of small objects**, each independently addressable:

```
dataset.zarr/
  .zmetadata          ← single JSON file, all metadata
  sst_anom_mosaic/
    0.0               ← chunk: time=0, spatial tile 0
    0.1               ← chunk: time=0, spatial tile 1
    1.0               ← chunk: time=1, spatial tile 0
    ...
```

On S3, each chunk is a separate S3 object. Opening a dataset is a **single HTTP request** for `.zmetadata`. Reading a variable chunk is **one HTTP request** for that exact chunk — no B-tree traversal, no scattered seeks.

### Data transfer comparison for one day's data

| Format | HTTP requests | Data transferred | Useful data |
|---|---|---|---|
| NetCDF4 | ~70+ | ~50 MB (scattered blocks from 101 MB file) | ~5 MB |
| Zarr (5, full\_grid) chunks | 1 | ~25 MB (5 days of variable, compressed) | ~5 MB |
| Zarr (1, full\_grid) chunks | 1 | ~5 MB (1 day of variable, compressed) | ~5 MB |

NetCDF4's scattered block fetching pulls in large portions of the 101 MB file that belong to other variables or other time steps — data we never use. Zarr fetches **exactly and only the chunks requested**.

### Chunk shape matters

Zarr chunk shape directly controls how much data is fetched per request. For a tile server with single-day access:

- `(5, full_grid)` — fetches 5 days per request, 5× wasted data, still fine in-region
- `(1, full_grid)` — fetches exactly 1 day per request, optimal for this use case

The chunk shape should match the access pattern. Since this tile server always reads one product for one date, `(1, full_grid)` minimises both data transfer and latency everywhere.

---

## Summary

| | NetCDF3 | NetCDF4 | Zarr |
|---|---|---|---|
| Metadata location | Single header | Scattered throughout file | Single `.zmetadata` object |
| Compression | No | Yes (per chunk) | Yes (per chunk) |
| Chunking | No | Yes | Yes |
| Cloud-friendly | Somewhat (header only) | No | Yes (designed for it) |
| HTTP requests to open | 1 | ~70+ | 1 |
| HTTP requests to read one variable | 1 | 1–few | 1 per chunk |
| Cold load at home (100 MB file) | N/A | ~32s | ~3s |
| Cold load in AWS in-region | N/A | ~120ms | ~6ms |

NetCDF4 is not a worse format than NetCDF3 — it added compression and chunking which are essential for large scientific datasets. It simply predates cloud object storage and was not designed for it. Zarr takes the same ideas (compression, chunking) and builds them around cloud-native access patterns.

For this tile server serving IMOS ocean data products, **Zarr with `(1, full_grid)` chunk shape is the correct long-term solution**.

---

## IMOS Product File Analysis

### File structure comparison

| | OceanCurrent GSLA/NRT | AusTemp ssta | AusTemp Marine Heatwave |
|---|---|---|---|
| Grid | 351 × 641 | 1,890 × 2,685 | 2,000 × 3,900 |
| Pixels per variable | ~225K | ~5.1M | ~7.8M |
| Data variables | 6 | 5 | 15 |
| Dimension names | `TIME`, `LATITUDE`, `LONGITUDE` (uppercase) | `time`, `lat`, `lon` | `time`, `lat`, `lon` |
| Time encoding | Float64, days since 1985-01-01 | Int32, seconds since 1981-01-01 | Int32, seconds since 1981-01-01 |
| Variable storage | Int16 + scale\_factor | Float32 | Float32 |
| File size (approx) | ~5 MB | ~101 MB | ~95 MB |

### Observed cold-start times (home internet, ~200ms round-trip latency)

| | `open_dataset` | variable read | tile total |
|---|---|---|---|
| OceanCurrent | negligible | negligible | ~900ms |
| ssta | ~16s | ~15s | ~30s |
| Marine Heatwave | ~60s+ | ~30s+ | 90s+ |

**Why OceanCurrent is fast**: tiny grid (225K pixels) + small file → shallow HDF5 B-tree → few HTTP round-trips.

**Why Marine Heatwave is much slower than ssta despite being a smaller file (~95 MB vs ~101 MB)**: compressed file size is a poor predictor of `open_dataset` time. ssta stores continuous Float32 SST values (high entropy, compresses poorly → large file). Marine Heatwave stores categorical/sparse fields like `MHW_category` (0–4 scale), `dhdc` (count days), and large NaN regions (low entropy, compresses very well → smaller file despite more data).

What drives `open_dataset` time is the **number of HDF5 B-tree nodes**, which scales with variables × grid size, not compressed bytes:

`open_dataset time ∝ num_variables × grid_size × round_trip_latency`

Marine Heatwave: 15 vars × 7.8M pixels = 117M — ssta: 5 vars × 5.1M pixels = 25.5M. Nearly 5× more structural complexity despite the smaller file.

### Dimension and encoding notes

- OceanCurrent uses uppercase dimension names (`TIME`, `LATITUDE`, `LONGITUDE`). The loader renames these to lowercase via `COORD_NAMES` before any downstream processing.
- All three products encode time as a numeric offset (days or seconds since an epoch). xarray's default `decode_times=True` correctly interprets all variants — no product-specific time handling is needed.
- OceanCurrent variables are stored as Int16 with a `scale_factor`. xarray auto-applies the scaling on first `.values` access.

### When Zarr is necessary vs. when NetCDF is acceptable

| Deployment | OceanCurrent | ssta | Marine Heatwave |
|---|---|---|---|
| Home internet | Fine (fast file) | Slow cold start (~30s), cached after | Very slow cold start (90s+), cached after |
| AWS ap-southeast-2 (in-region) | Fast | ~1-2s cold start | ~2-4s cold start |

**Conclusion**: For in-region AWS deployment, NetCDF cold-start times are acceptable for all products — the LRU cache means the cost is paid at most once per product per date per server session. Zarr becomes necessary when:
- The server runs outside AWS (e.g. home internet, other cloud regions)
- Cold-start latency must be consistently low (e.g. first request per date cannot be slow)
- The data changes frequently enough that the LRU cache doesn't stay warm
