# Tile Server Implementation Plan

## Architecture

Two parallel stacks — NetCDF on-demand (`/tiles`) and Zarr (`/zarr`) — sharing product config and PNG encoding logic.

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
               │   routers/tiles.py   │   │ routers/zarr_tiles.py │
               │   /tiles             │   │ /zarr                 │
               └──────┬──────────┬────┘   └─────┬──────────┬──────┘
                      │          │               │          │
          ┌───────────▼──┐  ┌────▼──────────┐  ┌▼──────────────┐  ┌──────────────────┐
          │  loader.py   │  │ renderer.py   │  │zarr_loader.py │  │zarr_renderer.py  │
          │              │  │               │  │               │  │                  │
          │ load_dataset │  │ render_tile   │  │ load_zarr_    │  │ render_zarr_tile │
          │              │  │ render_       │  │ slice         │  │ render_zarr_     │
          │ dataset cache│  │ manifest      │  │               │  │ manifest         │
          │ LRU(10)      │  │               │  │ slice cache   │  │                  │
          │ (product,date│  │ processed     │  │ LRU(20)       │  │ processed cache  │
          │              │  │ cache LRU(20) │  │(url,date,vars)│  │ LRU(20)          │
          └──────┬───────┘  └───────────────┘  └──────┬────────┘  └──────────────────┘
                 │ miss                               │ miss
        ┌────────▼────────┐               ┌───────────▼────────┐
        │    AWS S3       │               │      AWS S3         │
        │  NetCDF per     │               │   Zarr store        │
        │  date           │               │   (all dates)       │
        └─────────────────┘               └────────────────────┘
```

**Request flows — NetCDF `/tiles/netcdf`**

```
manifest → load_dataset (S3 open + metadata) → render_manifest (reads variable data → S3 download) → respond

tile (warm) → load_dataset (cache hit) → _get_processed (cache hit) → _extract_chunk → PNG encode

tile (cold) → load_dataset → _get_processed (resample + normalise) → _extract_chunk → PNG encode
```

**Request flows — Zarr `/tiles/zarr`**

```
manifest → load_zarr_slice (store open once; .compute() downloads TIME chunk from S3) → render_zarr_manifest → respond

tile (warm) → load_zarr_slice (cache hit) → _get_zarr_processed (cache hit) → _extract_chunk → PNG encode

tile (cold) → load_zarr_slice (.compute() → S3) → _get_zarr_processed (resample + normalise) → _extract_chunk → PNG encode
```

---

## URL contract

```
GET /tiles/netcdf/{product_id}/{date}/{z}/{x}/{y}.png   → RGBA PNG tile
GET /tiles/netcdf/{product_id}/{date}/manifest.json     → bounds + value ranges + LOD grid config

GET /tiles/zarr/{product_id}/{date}/{z}/{x}/{y}.png     → RGBA PNG tile
GET /tiles/zarr/{product_id}/{date}/manifest.json       → bounds + value ranges + LOD grid config
```

`z` = LOD level, `x` = chunk column (0 = westernmost), `y` = chunk row (0 = northernmost). Not Web Mercator — custom atlas grid in geographic (lat/lon) space.

**Products**

| stack | product_id | variable | LODs |
|---|---|---|---|
| `/tiles/netcdf` | `ocean_current_gsla_ucur_vcur` | UCUR/VCUR | 1 |
| `/tiles/netcdf` | `ocean_current_gsla_gsla` | GSLA | 1 |
| `/tiles/netcdf` | `austemp_sst_anomaly_sst_anom_mosaic` | SST anomaly | 1/2/3 |
| `/tiles/netcdf` | `ausTemp_marine_heatwave_aus_dhd_mosaic` | DHD | 1/2/3 |
| `/tiles/netcdf` | `ausTemp_marine_heatwave_aus_ssta_mosaic` | SSTA | 1/2/3 |
| `/tiles/zarr` | `zarr_sea_level_anomaly` | GSLA | 1 |
| `/tiles/zarr` | `zarr_ocean_current` | UCUR/VCUR | 1 |

---

## File structure

```
titiler-project/
  main.py                      ← both routers wired up, CORS middleware, titiler COG router
  constants.py                 ← Product dataclass + 5 NetCDF products + 2 Zarr products
  routers/
    netcdf_tiles.py            ← /tiles/netcdf endpoints
    zarr_tiles.py              ← /tiles/zarr endpoints
  services/
    loader.py                  ← NetCDF: S3 open → lazy xr.Dataset → LRU dataset cache
    renderer.py                ← NetCDF: processed grid cache + chunk extract + PNG encode
    zarr_loader.py             ← Zarr: singleton store open + per-date slice cache (.compute())
    zarr_renderer.py           ← Zarr: processed grid cache + chunk extract + PNG encode
```

---

## Caching strategy

All caches are in-memory LRU (cachetools), evicted least-recently-used. Nothing written to disk.

### NetCDF cache layers

**Layer 1 — Dataset cache** (`services/netcdf_loader.py`, keyed `(product_id, date)`, maxsize=10)

Stores the lazy `xr.Dataset` from `xr.open_dataset(..., engine="h5netcdf")`. Holds HDF5 metadata and an open s3fs file handle — no variable data yet. A hit saves the S3 file-open cost and, critically, ensures every request for the same `(product, date)` receives the **same Python object**. This is the foundation the two layers below depend on.

**Layer 2 — xarray internal numpy cache** (implicit, no explicit code)

xarray stores numpy arrays on the `DataArray` object after the first `.values` access. Because Layer 1 returns the same `ds` object to every request, the first endpoint to touch a variable (whether `render_manifest`'s `.min().values` or `_get_processed`'s `ds.interp(...)`) loads the data from S3 and stores it on `ds`. All subsequent accesses — by any endpoint — read from that in-memory numpy array with no S3 I/O.

This is why hitting either endpoint first warms the other:
- `manifest → tile`: manifest's min/max calls load the variable into memory; tile's resample is pure CPU.
- `tile → manifest`: tile's resample loads the variable; manifest's min/max is instant numpy.

**Layer 3 — Processed grid cache** (`services/netcdf_renderer.py`, keyed `(id(ds), lod)`, maxsize=20)

Stores the resampled + normalised numpy arrays after `_resample_to_grid` and normalisation: `(val_24, ocean)` for scalar products, `(u_norm, v_norm, ocean)` for ocean current. A hit reduces per-tile work to `_extract_chunk` + PNG encode only. `id(ds)` is a stable key because `ds` is held alive by Layer 1.

### Zarr cache layers

Zarr has no Layer 2 equivalent because `.compute()` is called upfront — the slice is already fully materialised numpy when it leaves the loader.

**Layer 1 — Store singleton** (`services/zarr_loader.py`, `_stores` dict keyed by URL)

Caches the open `xr.Dataset` Zarr store (lazy metadata only). Avoids re-reading Zarr metadata from S3 on every request. Shared across all products that use the same store URL.

**Layer 2 — Slice cache** (`services/zarr_loader.py`, keyed `(store_url, date, variables)`, maxsize=20)

Stores a fully-computed (`xr.Dataset.compute()`) 2D lat×lon slice for one date and variable set. The `.compute()` call reads the full Zarr TIME chunk from S3 (~9 MB for GSLA, ~18 MB for UCUR+VCUR, due to chunk size of 5 time steps). The result is entirely in-memory numpy — no lazy loading remains. Keyed by `variables` so `ZARR_SEA_LEVEL_ANOMALY` and `ZARR_OCEAN_CURRENT` cache independently and only fetch their own variables.

**Layer 3 — Processed grid cache** (`services/zarr_renderer.py`, keyed `(id(ds), lod)`, maxsize=20)

Same role as NetCDF Layer 3. `id(ds)` is stable because `ds` is held alive by Layer 2.

### Cost eliminated by each layer

| Layer | NetCDF | Zarr |
|---|---|---|
| L1 | S3 file open + HDF5 metadata read; stable `ds` object for L2/L3 | Zarr metadata re-read from S3 |
| L2 | S3 variable data download (lazy→numpy on first access, shared via same `ds`) | S3 Zarr chunk reads (`.compute()` result held in RAM) |
| L3 | CPU bilinear resample over full LOD grid | CPU bilinear resample over full LOD grid |

### Thread safety

All caches use `threading.Lock` (FastAPI runs sync endpoints in a thread pool). The processed grid caches additionally track in-flight computations with a `threading.Event` per key: if two threads request the same `(ds, lod)` simultaneously, the second waits for the first rather than computing a duplicate. This ensures the resample runs only once per `(date, lod)` even under concurrent requests.

---

## Performance

### What takes the time

**The S3 variable data download dominates on cold start, not the resample.**

For `austemp_sst_anomaly_sst_anom_mosaic`, the first `manifest.json` takes ~7s. The breakdown:

1. `load_dataset` opens the S3 file and reads HDF5 metadata — fast, no variable data yet.
2. `render_manifest` calls `ds["sst_anom_mosaic"].min().values` — this is the first `.values` access on the lazy h5netcdf dataset, triggering s3fs to download the actual variable data from S3. **This is the ~7s.**
3. The first tile request triggers `_get_processed` → `_resample_to_grid`. The variable bytes are now in the s3fs block cache, so the resample reads from RAM and is fast.
4. All subsequent tiles at the same LOD hit `_processed_cache` and return near-instantly.

**Why `_cache` (dataset cache) alone is not enough**: it stores a lazy dataset with an open file handle. The variable data is not in the cache itself — it lives on the `ds` object after the first `.values` access (xarray's internal numpy cache, Layer 2). If `ds` is evicted from `_cache` and a new object is created, Layer 2 is cold again and a fresh S3 read is needed. Only `_processed_cache` (Layer 3) guarantees fully in-RAM resampled arrays regardless of xarray's internal state.

### Cold start comparison: NetCDF vs Zarr

| | NetCDF | Zarr |
|---|---|---|
| Data open | Metadata only (lazy) | Metadata only (lazy) |
| Variable data fetch | Lazy — triggered on first `.values` access, only the needed variable | Eager — `.compute()` downloads full TIME chunk (5 dates × all variables) |
| Cold start cost | ~1 variable × 1 date of bytes | ~3 variables × 5 dates of bytes (~27 MB) |
| Warm (both caches hit) | Identical — only `_extract_chunk` + PNG encode |

---

## Approach comparison

| | Batch scripts | NetCDF on-demand (current) | Multiscale Zarr (future) |
|---|---|---|---|
| Pre-computation | All tiles pre-generated as PNG | None | Multiscale Zarr per date written to S3 |
| Server work per tile (cold) | Static file serve | S3 variable read + resample + encode | Spatial bbox read + encode |
| Server work per tile (warm) | Static file serve | encode only | encode only |
| Resampling | At preparation time | At request time (cached after first) | At preparation time |
| Storage | NetCDF + all PNG tiles | NetCDF only | NetCDF + Zarr stores |
| Flexibility | Only pre-generated dates | Any date on-demand | Any date after conversion |

Batch scripts are fastest for serving (pre-generated PNGs, no server computation). The on-demand server trades cold-start latency for storage efficiency and flexibility. Multiscale Zarr would eliminate the resample entirely by storing pre-built resolution levels — the conversion step is the existing batch scripts rewritten to output multiscale Zarr to S3.

---

## Out of scope

- `data.json` endpoint
- Authentication / rate limiting
- Multiscale Zarr
