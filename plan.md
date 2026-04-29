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
          │              │  │ cache LRU(20) │  │ (date)        │  │ LRU(20)          │
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

### Three cache layers

**1. Dataset cache** — `services/loader.py`, keyed `(product_id, date)`, maxsize=10

Stores the lazy `xr.Dataset` returned by `xr.open_dataset(..., engine="h5netcdf")`. This is lightweight — it holds HDF5 metadata, coordinate arrays, and an open s3fs file handle. The actual variable data arrays are **not** in this cache; they live in s3fs's internal block cache once first read. A cache hit saves the S3 file-open + HDF5 metadata cost.

**2. Zarr slice cache** — `services/zarr_loader.py`, keyed `date`, maxsize=20

Stores a fully-computed (`xr.Dataset.compute()`) 2D lat×lon slice for one date. Unlike the dataset cache, this holds real numpy arrays in RAM — the `.compute()` call downloads the entire TIME chunk from S3 (~27 MB for 3 variables × 5 time steps at 351×641 points). Subsequent requests for the same date read purely from RAM.

**3. Processed grid cache** — `services/renderer.py` and `zarr_renderer.py`, keyed `(id(ds), lod)`, maxsize=20 each

Stores the final resampled + normalised numpy arrays: `(val_24, ocean)` for scalar products, `(u_norm, v_norm, ocean)` for ocean current. This is the cache that makes tiles fast — after a hit, per-tile work is only `_extract_chunk` + PNG encode with no S3 I/O or resampling. `id(ds)` is safe as a key because `ds` is held alive by its respective upstream cache.

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

**Why `_cache` (dataset cache) alone is not enough**: it stores a lazy dataset with an open file handle, not the variable data. If the s3fs block cache is warm, repeated reads are fast. If evicted, the variable data is re-fetched from S3 even on a dataset cache hit. Only `_processed_cache` guarantees fully in-RAM numpy arrays.

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
