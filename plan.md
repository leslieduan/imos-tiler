# Tile Server Implementation Plan

## Architecture

```
                        ┌─────────────────────────────────────────┐
                        │              Frontend (WebGL)            │
                        └──────────────┬──────────────────────────┘
                                       │ HTTP
                        ┌──────────────▼──────────────────────────┐
                        │               FastAPI app                │
                        │                 main.py                  │
                        └──────────────┬──────────────────────────┘
                                       │
                        ┌──────────────▼──────────────────────────┐
                        │           routers/tiles.py               │
                        │                                          │
                        │  GET /{product_id}/{date}/{z}/{x}/{y}.png│
                        │  GET /{product_id}/{date}/manifest.json  │
                        │            + prewarm trigger             │
                        └──────┬───────────────────┬──────────────┘
                               │                   │
               ┌───────────────▼──────┐  ┌─────────▼──────────────┐
               │  services/loader.py  │  │  services/renderer.py  │
               │                      │  │                        │
               │  load_dataset()       │  │  render_tile()         │
               │                      │  │  render_manifest()     │
               │  ┌────────────────┐  │  │                        │
               │  │  Dataset cache │  │  │  ┌──────────────────┐  │
               │  │  LRU maxsize=10│  │  │  │ Processed cache  │  │
               │  │  key:          │  │  │  │ LRU maxsize=20   │  │
               │  │  (product,date)│  │  │  │ key: (id(ds),lod)│  │
               │  └───────┬────────┘  │  │  └────────┬─────────┘  │
               └──────────┼───────────┘  └───────────┼────────────┘
                          │                           │
                          │ cache miss                │ cache miss
                          │                           │
               ┌──────────▼───────────┐  ┌───────────▼────────────┐
               │       AWS S3         │  │  resample + normalise  │
               │  (anonymous, IMOS)   │  │                        │
               │  NetCDF via s3fs     │  │  ds.interp() (scipy)   │
               │  + h5netcdf engine   │  │  nan_to_num / clip     │
               └──────────────────────┘  │  → numpy arrays        │
                                         └────────────────────────┘
```

**Request flows**

Manifest request (first time for a date):
```
router → loader (S3 download + cache) → render_manifest → respond
                                      ↘ background thread → _get_processed (all LODs)
```

Tile request (after prewarm):
```
router → loader (cache hit) → render_tile → _get_processed (cache hit)
       → _extract_chunk → PNG encode → respond
```

Tile request (cold, no prewarm):
```
router → loader (cache hit or S3 download) → render_tile → _get_processed (resample + normalise)
       → _extract_chunk → PNG encode → respond
```

---

## URL contract

```
GET /tiles/{product_id}/{date}/{z}/{x}/{y}.png   → RGBA PNG tile
GET /tiles/{product_id}/{date}/manifest.json     → bounds + value ranges + LOD grid config
```

`z` = LOD level, `x` = cx (chunk column, 0 = westernmost), `y` = cy (chunk row, 0 = northernmost).  
Not Web Mercator — custom atlas grid in geographic (lat/lon) space.

---

## File structure

```
titiler-project/
  main.py                    ← tiles router wired up
  constants.py               ← Product dataclass + all 5 product configs + PRODUCTS dict
  routers/
    tiles.py                 ← 2 endpoints: tile PNG + manifest + prewarm trigger
  services/
    loader.py                ← S3 open → xr.Dataset → time-select → LRU cache
    renderer.py              ← resample cache + chunk extract + PNG encode
```

---

## Implementation

### Dependencies

`xarray`, `s3fs`, `Pillow`, `scipy`, `netCDF4`, `h5netcdf`, `h5py`, `cachetools`

`h5netcdf` + `h5py` are required because `netCDF4` does not support file-like objects from s3fs. `h5netcdf` does.

### `constants.py`

`Product` dataclass fields:
- `coord_names: dict` — rename map applied after `open_dataset`. GSLA files use `TIME`/`LATITUDE`/`LONGITUDE`; all others use `time`/`lat`/`lon`. Normalised in the loader so renderer always sees `time`/`lat`/`lon`.
- `use_isel_time: bool` — MHW files store time as a single Int32 Unix timestamp; must use `.isel(time=0)` instead of `.sel(time=date)`.

### `services/loader.py`

```python
load_dataset(product_id: str, date: str) -> xr.Dataset
```

- Opens NetCDF from S3 via `s3fs.S3FileSystem(anon=True)` + `engine="h5netcdf"`
- Applies `ds.rename(product.coord_names)` if set
- Time-selects with `.isel(time=0)` (MHW) or `.sel(time=date)` (all others)
- **LRU cache** (`maxsize=10`, thread-safe) keyed on `(product_id, date)` — the full S3 download happens once per date, all tile requests for that date reuse the cached dataset

### `services/renderer.py`

```python
render_tile(product, ds, lod, cx, cy) -> bytes
render_manifest(product, ds) -> dict
```

`render_tile` pipeline:
1. `_get_processed(product, ds, lod)` — resample full LOD grid + normalise to final numpy arrays
2. `_extract_chunk` — slice the requested cx/cy with 1-pixel padding + edge replication
3. PNG encode — 24-bit scalar (SSTA/MHW/SLA) or UV 8-bit (ocean current), `optimize=False`

**Processed grid cache** (`maxsize=20`, thread-safe) keyed on `(id(ds), lod)`:
- Combines the resample (`ds.interp()` via scipy) and all numpy ops (nan_to_num, clip, bit shift) into one cached step
- Stores final numpy arrays: `(val_24, ocean)` for scalar products, `(u_norm, v_norm, ocean)` for ocean current
- After a cache hit, per-tile work is only `_extract_chunk` + PNG encode — no full-grid operations
- `id(ds)` is stable because `ds` is held alive by the loader's LRU cache

### `routers/tiles.py`

**Tile endpoint** — validates product, lod, and cx/cy bounds, loads dataset, renders and returns PNG.

**Manifest endpoint** — returns manifest JSON, then fires a `daemon=True` background thread that calls `_get_processed` for every LOD of the product. The frontend always fetches the manifest first, so by the time the user starts requesting tiles all LODs are pre-warmed.

---

## Caching strategy

All caches are in-memory (RAM), held in the server process — nothing written to disk. Entries are evicted least-recently-used when the cache is full.

### Dataset cache (`services/loader.py`)

| | |
|---|---|
| Key | `(product_id, date)` |
| Value | Raw `xr.Dataset` from S3 |
| Size | `maxsize=10` |

Downloading a NetCDF from S3 is the single most expensive operation (~seconds). This cache ensures it happens once per date; every tile request for that date reuses the in-memory dataset.

### Processed grid cache (`services/renderer.py`)

| | |
|---|---|
| Key | `(id(ds), lod)` |
| Value | `(val_24, ocean)` for scalar products; `(u_norm, v_norm, ocean)` for ocean current |
| Size | `maxsize=20` (~1–2 dates × all products) |

Combines the resample and all numpy normalisation ops into one cached step. On a cache hit, per-tile work is only `_extract_chunk` + PNG encode — no full-grid operations at all. Storing final uint8/uint32 arrays is also more memory-efficient than storing the intermediate float64 resampled dataset (~27 MB vs ~44 MB for SSTA LOD3).

`id(ds)` is safe as a key because the dataset object is kept alive by the dataset cache above.

Sizing: all 5 products for one date consume ~11 slots (3 LODs × 3 SSTA/MHW products + 1 LOD × 2 GSLA products). `maxsize=20` comfortably holds one active date. Increasing to 30–40 would cover 3–4 dates at the cost of ~300–400 MB RAM.

### Thread safety

Both caches use a `threading.Lock` (FastAPI runs sync endpoints in a thread pool). The processed grid cache additionally tracks in-flight computations via a `threading.Event` per key: if two threads request the same `(ds, lod)` simultaneously, the second waits for the first to finish rather than computing a duplicate. This is what makes pre-warming effective.

### Pre-warming

The manifest endpoint fires a `daemon=True` background thread that calls `_get_processed` for every LOD of the product immediately after responding. Since the frontend always fetches the manifest before requesting tiles, all LODs are warm before the first tile arrives. Without this, the first tile at each zoom level would pay the full resample + normalisation cost.

---

## Performance profile

| Request | Cost |
|---|---|
| First request for a date (any product) | Slow — S3 download + resample for that LOD |
| Subsequent tiles, same date + same z | Fast — both caches hit, only chunk extract + PNG encode |
| First tile at a new z (same date) | Medium — resample cache miss for that LOD, dataset cache hit |
| After manifest fetch (prewarm complete) | All LODs warm — every tile fast |

---

## Resampling bottleneck: how production tilers solve it

`_resample_to_grid` is the core performance bottleneck. It runs `ds.interp()` (scipy bilinear interpolation) over the full LOD grid — e.g. 5.5M pixels for SSTA LOD3 — because the source NetCDF grid points don't align with tile pixel positions. This is unavoidable given the current data format.

### How production titiler avoids it: COG

Production titiler is fast because it uses **Cloud-Optimized GeoTIFF (COG)**, a format with a pre-built multi-resolution overview pyramid:

```
COG file (S3)
  zoom 10  →  pre-tiled 256×256 blocks at full resolution
  zoom 9   →  pre-tiled at 1/2 resolution
  zoom 8   →  pre-tiled at 1/4 resolution  (pre-resampled at write time)
  ...
```

At request time, GDAL/rasterio picks the matching overview level, issues a single HTTP range request for just those bytes, and returns — **no resampling, no full file download**. The resampling happened once at data preparation time, not at request time.

### Our current approach

NetCDF has no overview pyramid. We must download the whole file and resample at request time via scipy. We mitigate this with:
- **Dataset cache** — download once per date
- **Processed grid cache** — resample once per (date, lod), reuse for all tiles at that zoom
- **Parallel prewarm** — all LODs resample concurrently in background after manifest fetch

This keeps the server usable but the first resample per LOD is still slow (~seconds for large grids).

### Future: multiscale Zarr

Zarr can achieve the same result as COG overviews by storing **multiple resolution levels explicitly** at conversion time:

```
ssta_2026-01-15.zarr/
  0/   ← full resolution  (LOD3: 2880×1920)
  1/   ← 1/2 resolution   (LOD2: 1440×960)
  2/   ← 1/4 resolution   (LOD1: 720×576)
```

At request time: open the Zarr store (near-instant, metadata only), select the right resolution level, read only the spatial chunks for the tile bbox — no full-dataset load, no resample.

The conversion step is essentially the existing batch scripts rewritten to output multiscale Zarr to S3 instead of PNG chunks to disk.

### Comparison across all approaches

| | Batch scripts (current) | NetCDF on-demand (current) | Multiscale Zarr (future) |
|---|---|---|---|
| Pre-computation | All tiles pre-generated as PNG | None | Multiscale Zarr per date written to S3 |
| Server work per tile | Static file serve | S3 download + resample + encode | Spatial bbox read + encode |
| Resampling | At preparation time | At request time (cached) | At preparation time |
| Storage | NetCDF + all PNG tiles | NetCDF only | NetCDF + Zarr stores |
| Flexibility | Only pre-generated dates | Any date on-demand | Any date after conversion |
| Caches needed | None (nginx) | Dataset + processed grid + prewarm | None needed |
| Tooling | xarray/PIL | xarray/scipy/PIL | xarray/zarr/PIL |

Batch scripts are still the fastest serving approach (pre-generated PNGs, no server computation). The on-demand tile server trades serving speed for storage efficiency and flexibility. Multiscale Zarr sits in between: pre-compute the resampling, defer only PNG encoding to request time.

---

## Out of scope (this pass)

- `data.json` endpoint
- Authentication / rate limiting
- Zarr migration
