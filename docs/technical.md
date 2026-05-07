# Technical Reference

## Decision: Migrate All Products to Zarr

The NetCDF/HDF5 stack has an unacceptable cold-start cost for cloud-native serving. HDF5 B-tree traversal requires hundreds of sequential HTTP round-trips regardless of what code does — it is a file format constraint, not fixable in the application layer. Observed cold starts from home internet: ssta ~30s, Marine Heatwave 90s+ (8m 34s TTFB measured). Even in-region on AWS, Marine Heatwave takes 2–4s on cold start due to its 15 variables × 7.8M pixel grid.

Zarr eliminates this entirely: metadata is one `.zmetadata` HTTP request, and variable chunks are directly addressable with no traversal.

**Full format analysis and IMOS product file details: `docs/netcdf-vs-zarr.md`.**

### Migration status

| Product               | Variable(s)             | NetCDF source                 | Zarr store                                                            | Status                                                              |
|-----------------------|-------------------------|-------------------------------|-----------------------------------------------------------------------|---------------------------------------------------------------------|
| Sea level anomaly     | GSLA                    | `OceanCurrent/GSLA/NRT`       | `model_sea_level_anomaly_gridded_realtime.zarr`                       | ✓ Zarr ready                                                        |
| Ocean current         | UCUR, VCUR              | `OceanCurrent/GSLA/NRT`       | `model_sea_level_anomaly_gridded_realtime.zarr`                       | ✓ Zarr ready                                                        |
| Radar wind (SA Gulfs) | WDIR                    | —                             | `radar_SouthAustraliaGulfs_wind_delayed_qc.zarr`                      | ✓ Zarr ready                                                        |
| SST anomaly           | sst_anom_mosaic         | `SRS/AusTemp/ssta`            | —                                                                     | Zarr store needed                                                   |
| Marine Heatwave DHD   | dhd_mosaic              | `SRS/AusTemp/Marine-Heatwave` | —                                                                     | Zarr store needed                                                   |
| Marine Heatwave SSTA  | ssta_mosaic             | `SRS/AusTemp/Marine-Heatwave` | —                                                                     | Zarr store needed                                                   |
| SST (GHRSST)          | sea_surface_temperature | —                             | `satellite_ghrsst_l3s_1day_nighttime_multi_sensor_australia.zarr`     | ✗ Excluded — inconsistent TIME dimension sizes across variables     |

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
cold  → get_lod_grids (opens store, computes, writes product.lod_grids) → load_zarr_slice (.compute()) → _get_zarr_processed (resample) → _extract_chunk → PNG encode
warm  → get_lod_grids (product.lod_grids already set)                   → load_zarr_slice (cache hit)  → _get_zarr_processed (cache hit) → _extract_chunk → PNG encode
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
  constants.py                   ← Product dataclass + LOD algorithm; NetCDF products (5) + Zarr products (3)
                                    LOD_ZOOM_THRESHOLDS, DEFAULT_ZARR_LOD_GRIDS, MAX_LODS, MIN_COARSEST_GRID
  docs/
    technical.md                 ← this file
    dataset.md                   ← per-store variable/dimension/chunking reference
    netcdf-vs-zarr.md            ← format comparison, IMOS product file analysis, performance data
  routers/
    netcdf_tiles.py              ← /tiles/netcdf  (legacy)
    zarr_tiles.py                ← /tiles/zarr    (primary)
  services/
    netcdf_loader.py             ← S3 open → lazy xr.Dataset, LRU dataset cache
    netcdf_renderer.py           ← processed grid cache + chunk extract + PNG encode
    zarr_loader.py               ← Zarr store singleton + per-(date, variables) slice cache + get_lod_grids
    zarr_renderer.py             ← processed grid cache + chunk extract + PNG encode
```

---

## LOD grid system

### Constants (`constants.py`)

- `MAX_LODS = 4` — frontend WebGL atlas limit: at most 4 LOD levels per product
- `MIN_COARSEST_GRID = (2, 2)` — minimum (cols, rows) for the coarsest LOD level; levels below this are dropped. If all levels are filtered out (data smaller than one chunk), falls back to the native finest grid so there is always at least one LOD.
- `LOD_ZOOM_THRESHOLDS: dict[int, int]` — universal map zoom thresholds applied to all products (e.g. `{2: 4, 3: 5, 4: 6}`)
- `DEFAULT_ZARR_LOD_GRIDS` — fallback used when lat/lon dims cannot be resolved from the store

### Algorithm (`Product._compute_lod_grids` in `constants.py`)

Derives LOD grids from actual data dimensions and chunk size. Accepts `max_lods` and `min_coarsest` as parameters (defaulting to the constants above).

1. Finest level: `ceil(data_width / chunk_w) × ceil(data_height / chunk_h)`
2. Depth: `floor(log2(max(finest_cols, finest_rows)))` — number of halvings before both axes reach 1 (uses `max` so elongated grids go as deep as the wider axis allows)
3. Each level `k`: `(ceil(finest_cols / 2^k), ceil(finest_rows / 2^k))` — `ceil` preserves coverage at intermediate scales (e.g. `finest=5` → `3, 2` not `2, 1`)
4. Drop levels whose cols or rows fall below `min_coarsest`. If nothing remains (data fits within a single chunk), fall back to `(finest_cols, finest_rows)` directly.
5. Take the finest `max_lods` levels; assign LOD indices starting at 1 (coarsest)

Example: `Product._compute_lod_grids(3000, 1500, (256, 256))` → `{1: (3, 2), 2: (6, 3), 3: (12, 6)}`

Small dataset example (radar SA Gulfs, 102×74, chunk 240×192): finest=(1,1), filtered to nothing, fallback → `{1: (1, 1)}`

### Lazy population for Zarr products (`zarr_loader.py` — `get_lod_grids`)

Zarr products are defined in `constants.py` with `lod_grids={}`. On the first request:

1. `get_lod_grids(product)` checks `product.lod_grids` — empty, so proceeds
2. Opens the Zarr store (singleton — reused across all calls to the same URL)
3. Reads lat/lon dimension sizes from store metadata (`.zmetadata`, no data fetch)
4. Calls `Product._compute_lod_grids` and writes the result back via `object.__setattr__` (bypasses `frozen=True` for this one field only)
5. All subsequent calls return immediately from the `if product.lod_grids` guard

NetCDF products have `lod_grids` hardcoded in `constants.py` — `get_lod_grids` is not used for them.

---

## Coordinate normalisation

On store open, `_get_store` applies `COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}` to rename any uppercase coordinate names to lowercase. This happens once per store URL and is stored in the singleton. All downstream code (renderer, manifest, point endpoint) can always assume `lat`/`lon`/`time` regardless of what the store uses natively.

---

## Caching strategy

All caches are in-memory LRU (cachetools), evicted least-recently-used. Nothing written to disk.

### Zarr cache layers

**Layer 1 — Store singleton** (`zarr_loader.py`, `_stores` dict keyed by URL)

Caches the open Zarr store handle (lazy, metadata only). One HTTP request per store URL ever. Shared across all products using the same store.

**Layer 2 — Slice cache** (`zarr_loader.py`, keyed `(store_url, date, variables)`, maxsize=20)

Stores a fully-computed (`.compute()`) 2D lat×lon numpy slice. This is the only S3 data read in the Zarr path — one chunk fetch per cold (date, variable) pair. Keyed by `variables` so different products using the same store cache independently.

**Layer 3 — Processed grid cache** (`zarr_renderer.py`, keyed `(id(ds), lod)`, maxsize=20)

Stores the resampled + normalised numpy arrays for the full LOD grid. A hit reduces per-tile work to `_extract_chunk` + PNG encode only — no S3 I/O, no resampling. `id(ds)` is stable because `ds` is held alive by Layer 2.

### NetCDF cache layers (legacy)

**Layer 1 — Dataset cache** (`netcdf_loader.py`, keyed `(source_path, date)`, maxsize=10)

Caches the lazy `xr.Dataset`. Keyed by `source_path` so products sharing the same S3 file share one `ds` object and pay HDF5 traversal only once.

**Layer 2 — xarray internal numpy cache** (implicit)

xarray caches numpy arrays on the `DataArray` object after the first `.values` access. Because Layer 1 returns the same `ds` Python object, whichever endpoint runs first warms it for the other.

**Layer 3 — Processed grid cache** (`netcdf_renderer.py`, keyed `(id(ds), var_key, lod)`, maxsize=20)

Same role as Zarr Layer 3. `var_key` is included because multiple products can share the same `ds` (same source file, different variables).

### Thread safety

All caches use `threading.Lock`. The processed grid caches additionally use a `threading.Event` per in-flight key: concurrent requests for the same `(ds, lod)` wait for the first computation to complete rather than duplicating it.

---

## Performance

The only bottleneck is **reading the source file on a cold start**. Everything else — resampling, normalisation, PNG encoding — is fast CPU work.

|                       | Zarr (in-region AWS)                                         | Zarr (home internet) | NetCDF (in-region AWS) | NetCDF (home internet) |
|-----------------------|--------------------------------------------------------------|----------------------|------------------------|------------------------|
| Store/file open       | ~6ms (1 req)                                                 | ~200ms (1 req)       | ~70ms–2s (70+ reqs)    | ~16s–60s+ (70+ reqs)   |
| Variable data read    | ~1 chunk (~10–20 MB)                                         | ~3–7s                | ~1 variable            | ~15s–30s+              |
| Warm (all caches hit) | `_extract_chunk` + PNG encode only — identical for all cases |

For detailed analysis of why NetCDF cold starts are slow and product-by-product breakdown, see `docs/netcdf-vs-zarr.md`.

---

## PNG encoding contract

Tiles are RGBA PNGs (`optimize=False`). The byte layout is fixed and consumed by a WebGL shader:

- **24-bit scalar** (GSLA, SSTA, DHD, SLA, WDIR): R=high byte, G=mid byte, B=low byte of normalised uint24; A=ocean mask (255=ocean, 0=land, premultiplied).
- **Ocean current** (UV): R=U normalised to 8-bit, G=V normalised to 8-bit, B=ocean mask×255, A=255.

Normalisation ranges (`val_min`/`val_max`, `u_min`/`u_max`, etc.) are computed from the full pre-resampled dataset and returned in `manifest.json`. All tiles for a date share the same ranges.

---

## Out of scope

- Authentication / rate limiting
- Multiscale Zarr (would eliminate the resample by pre-storing resolution levels)
- AusTemp Zarr store conversion (needed to complete the migration)
