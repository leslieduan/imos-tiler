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

## Future: Zarr

Switching the source data from NetCDF to Zarr would improve S3 read performance significantly:

- **NetCDF (HDF5)**: single monolithic file, sequential HTTP range requests, whole file must be read before data is usable
- **Zarr**: each chunk is a separate S3 object, reads are parallel and lazy — only the chunks overlapping the requested region are fetched

With Zarr, a different rendering architecture becomes viable: read only the geographic bbox for each tile from S3 (instead of the full dataset), resample just that small region, and skip the resample cache entirely. Every tile request would be independently fast without prewarm.

| | NetCDF (current) | Zarr (future) |
|---|---|---|
| Dataset LRU cache | Critical | Not needed (`open_dataset` is near-instant) |
| Processed grid cache | Critical | Not needed if per-tile bbox read |
| Prewarm | Very useful | Not needed |
| Architecture | Load full dataset → resample + normalise full grid → extract chunk | Read tile bbox → resample small slice → encode |

Blocked on whether IMOS data is available in Zarr on S3, or requires conversion.

---

## Out of scope (this pass)

- `data.json` endpoint
- Authentication / rate limiting
- Zarr migration
