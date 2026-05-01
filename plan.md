# Tile Server Implementation Plan

## Decision: Migrate All Products to Zarr

The NetCDF/HDF5 stack has an unacceptable cold-start cost for cloud-native serving. HDF5 B-tree traversal requires hundreds of sequential HTTP round-trips regardless of what code does — it is a file format constraint, not fixable in the application layer. Observed cold starts from home internet: ssta ~30s, Marine Heatwave 90s+ (8m 34s TTFB measured). Even in-region on AWS, Marine Heatwave takes 2–4s on cold start due to its 15 variables × 7.8M pixel grid.

Zarr eliminates this entirely: metadata is one `.zmetadata` HTTP request, and variable chunks are directly addressable with no traversal.

**Full format analysis and IMOS product file details: `docs/netcdf-vs-zarr.md`.**

### Migration status

| Product              | Variable(s)     | NetCDF source                 | Zarr store                                      | Status            |
| -------------------- | --------------- | ----------------------------- | ----------------------------------------------- | ----------------- |
| Sea level anomaly    | GSLA            | `OceanCurrent/GSLA/NRT`       | `model_sea_level_anomaly_gridded_realtime.zarr` | ✓ Zarr ready      |
| Ocean current        | UCUR, VCUR      | `OceanCurrent/GSLA/NRT`       | `model_sea_level_anomaly_gridded_realtime.zarr` | ✓ Zarr ready      |
| SST anomaly          | sst_anom_mosaic | `SRS/AusTemp/ssta`            | —                                               | Zarr store needed |
| Marine Heatwave DHD  | dhd_mosaic      | `SRS/AusTemp/Marine-Heatwave` | —                                               | Zarr store needed |
| Marine Heatwave SSTA | ssta_mosaic     | `SRS/AusTemp/Marine-Heatwave` | —                                               | Zarr store needed |

---

## Architecture

Zarr is the primary stack. The NetCDF stack is retained for products not yet available as Zarr.

```
                    ┌──────────────────────────────────────┐
                    │           Frontend (WebGL)            │
                    └──────────────────┬───────────────────┘
                                       │ HTTP
                    ┌──────────────────▼───────────────────┐
                    │            FastAPI  main.py           │
                    └───────────────┬───────────┬───────────┘
                                    │           │
               ┌────────────────────▼─┐   ┌─────▼────────────────┐
               │  netcdf_tiles.py     │   │  zarr_tiles.py        │
               │  /tiles/netcdf       │   │  /tiles/zarr          │
               │  (legacy)            │   │  (primary)            │
               └──────┬──────────┬────┘   └─────┬──────────┬──────┘
                      │          │               │          │
          ┌───────────▼──────┐ ┌─▼────────────┐ ┌▼─────────────┐ ┌────────────────┐
          │ netcdf_loader.py │ │netcdf_       │ │zarr_loader.py│ │zarr_renderer.py│
          │                  │ │renderer.py   │ │              │ │                │
          │ dataset cache    │ │              │ │ store        │ │                │
          │ LRU(10)          │ │ processed    │ │ singleton    │ │ processed      │
          │ (source_path,    │ │ cache LRU(20)│ │ slice cache  │ │ cache LRU(20)  │
          │  date)           │ │ (id(ds),     │ │ LRU(20)      │ │ (id(ds),       │
          │                  │ │  var, lod)   │ │ (url,date,   │ │  lod)          │
          │                  │ │              │ │  variables)  │ │                │
          └──────┬───────────┘ └──────────────┘ └──────┬───────┘ └────────────────┘
                 │ miss                               │ miss
        ┌────────▼────────┐               ┌───────────▼────────┐
        │    AWS S3       │               │      AWS S3         │
        │  NetCDF/date    │               │   Zarr store        │
        │  (per file)     │               │   (all dates)       │
        └─────────────────┘               └────────────────────┘
```

**Request flows — Zarr `/tiles/zarr` (primary)**

```
cold  → load_zarr_slice (.compute() → 1 S3 chunk read) → _get_zarr_processed (resample) → _extract_chunk → PNG encode
warm  → load_zarr_slice (slice cache hit)               → _get_zarr_processed (cache hit) → _extract_chunk → PNG encode
```

**Request flows — NetCDF `/tiles/netcdf` (legacy)**

```
cold  → load_dataset (HDF5 traversal → S3) → _get_processed (variable read + resample) → _extract_chunk → PNG encode
warm  → load_dataset (cache hit)            → _get_processed (cache hit)                → _extract_chunk → PNG encode
```

---

## URL contract

```
GET /tiles/zarr/{product_id}/{date}/{z}/{x}/{y}.png     → RGBA PNG tile
GET /tiles/zarr/{product_id}/{date}/manifest.json       → bounds + value ranges + LOD grid config
GET /tiles/zarr/{product_id}/{date}/point?lat=&lon=     → variable value at point

GET /tiles/netcdf/{product_id}/{date}/{z}/{x}/{y}.png   → RGBA PNG tile  (legacy)
GET /tiles/netcdf/{product_id}/{date}/manifest.json     → (legacy)
GET /tiles/netcdf/{product_id}/{date}/point?lat=&lon=   → (legacy)
```

`z` = LOD level, `x` = chunk column (0 = westernmost), `y` = chunk row (0 = northernmost). Not Web Mercator — custom atlas grid in geographic (lat/lon) space.

---

## File structure

```
titiler-project/
  main.py                        ← both routers wired up, CORS middleware, titiler COG router
  constants.py                   ← Product dataclass; NetCDF products (5) + Zarr products (2)
  plan.md                        ← this file
  docs/
    netcdf-vs-zarr.md            ← format comparison, IMOS product file analysis, performance data
  routers/
    netcdf_tiles.py              ← /tiles/netcdf  (legacy)
    zarr_tiles.py                ← /tiles/zarr    (primary)
  services/
    netcdf_loader.py             ← S3 open → lazy xr.Dataset, LRU dataset cache
    netcdf_renderer.py           ← processed grid cache + chunk extract + PNG encode
    zarr_loader.py               ← Zarr store singleton + per-(date, variables) slice cache
    zarr_renderer.py             ← processed grid cache + chunk extract + PNG encode
```

---

## Caching strategy

All caches are in-memory LRU (cachetools), evicted least-recently-used. Nothing written to disk.

### Zarr cache layers

**Layer 1 — Store singleton** (`zarr_loader.py`, `_stores` dict keyed by URL)

Caches the open Zarr store handle (lazy, metadata only). One HTTP request per store URL ever. Shared across all products using the same store.

**Layer 2 — Slice cache** (`zarr_loader.py`, keyed `(store_url, date, variables)`, maxsize=20)

Stores a fully-computed (`.compute()`) 2D lat×lon numpy slice. This is the only S3 data read in the Zarr path — one chunk fetch per cold (date, variable) pair. With `(5, full_grid)` chunking, 5 dates worth of data is fetched (~9 MB for GSLA, ~18 MB for UCUR+VCUR). After this, the slice is entirely in RAM. Keyed by `variables` so different products using the same store cache independently and only fetch their own variables.

**Layer 3 — Processed grid cache** (`zarr_renderer.py`, keyed `(id(ds), lod)`, maxsize=20)

Stores the resampled + normalised numpy arrays for the full LOD grid. A hit reduces per-tile work to `_extract_chunk` + PNG encode only — no S3 I/O, no resampling. `id(ds)` is stable because `ds` is held alive by Layer 2.

### NetCDF cache layers (legacy)

**Layer 1 — Dataset cache** (`netcdf_loader.py`, keyed `(source_path, date)`, maxsize=10)

Caches the lazy `xr.Dataset` (HDF5 metadata only, no variable data). Keyed by `source_path` (not `product_id`) so products sharing the same S3 file (e.g. `dhd_mosaic` and `ssta_mosaic` from the same Marine Heatwave file) share one `ds` object and pay HDF5 traversal only once.

**Layer 2 — xarray internal numpy cache** (implicit)

xarray caches numpy arrays on the `DataArray` object after the first `.values` access. Because Layer 1 returns the same `ds` Python object to every caller, whichever endpoint runs first (manifest or tile) loads the variable data into memory and warms it for the other.

**Layer 3 — Processed grid cache** (`netcdf_renderer.py`, keyed `(id(ds), var_key, lod)`, maxsize=20)

Same role as Zarr Layer 3. `var_key` is included because multiple products can share the same `ds` (same file, different variables).

### Thread safety

All caches use `threading.Lock`. The processed grid caches additionally use a `threading.Event` per in-flight key: concurrent requests for the same `(ds, lod)` wait for the first computation to complete rather than duplicating it.

---

## Performance

The only bottleneck is **reading the source file on a cold start**. Everything else — resampling, normalisation, PNG encoding — is fast CPU work.

|                       | Zarr (in-region AWS)                                         | Zarr (home internet) | NetCDF (in-region AWS) | NetCDF (home internet) |
| --------------------- | ------------------------------------------------------------ | -------------------- | ---------------------- | ---------------------- |
| Store/file open       | ~6ms (1 req)                                                 | ~200ms (1 req)       | ~70ms–2s (70+ reqs)    | ~16s–60s+ (70+ reqs)   |
| Variable data read    | ~1 chunk (~10–20 MB)                                         | ~3–7s                | ~1 variable            | ~15s–30s+              |
| Warm (all caches hit) | `_extract_chunk` + PNG encode only — identical for all cases |

With `(5, full_grid)` Zarr chunking, each cold slice read fetches 5 dates of data instead of 1. On AWS in-region this is still well under 1s and is only paid once per (date, variable) pair per server session. The slice cache means all subsequent requests for that date are served from RAM regardless.

For detailed analysis of why NetCDF cold starts are slow and product-by-product breakdown, see `docs/netcdf-vs-zarr.md`.

---

## PNG encoding contract

Tiles are RGBA PNGs (`optimize=False`). The byte layout is fixed and consumed by a WebGL shader:

- **24-bit scalar** (GSLA, SSTA, DHD, SLA): R=high byte, G=mid byte, B=low byte of normalised uint24; A=ocean mask (255=ocean, 0=land, premultiplied).
- **Ocean current** (UV): R=U normalised to 8-bit, G=V normalised to 8-bit, B=ocean mask×255, A=255.

Normalisation ranges (`val_min`/`val_max`, `u_min`/`u_max`, etc.) are computed from the full pre-resampled dataset and returned in `manifest.json`. All tiles for a date share the same ranges.

---

## Out of scope

- Authentication / rate limiting
- Multiscale Zarr (would eliminate the resample by pre-storing resolution levels)
- AusTemp Zarr store conversion (needed to complete the migration)
