# Tile Server Implementation Plan

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
1. `_get_resampled(ds, product, lod)` — resample full LOD grid via `ds.interp()` (scipy linear)
2. `_extract_chunk` — slice the requested cx/cy with 1-pixel padding + edge replication
3. PNG encode — 24-bit scalar (SSTA/MHW/SLA) or UV 8-bit (ocean current), `optimize=False`

**Resample cache** (`maxsize=20`, thread-safe) keyed on `(id(ds), lod)`:
- `ds.interp()` over the full LOD grid (e.g. 2880×1920 for SSTA LOD3) is expensive
- Result is identical for all tiles sharing the same product+date+lod
- `id(ds)` is stable because `ds` is held alive by the loader's LRU cache
- First tile per (date, lod) is slow; all subsequent tiles at that lod are fast

### `routers/tiles.py`

**Tile endpoint** — validates product, lod, and cx/cy bounds, loads dataset, renders and returns PNG.

**Manifest endpoint** — returns manifest JSON, then fires a `daemon=True` background thread that calls `_get_resampled` for every LOD of the product. The frontend always fetches the manifest first, so by the time the user starts requesting tiles all LODs are pre-warmed.

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
| Resample cache | Critical | Not needed if per-tile bbox read |
| Prewarm | Very useful | Not needed |
| Architecture | Load full dataset → resample full grid → extract chunk | Read tile bbox → resample small slice → encode |

Blocked on whether IMOS data is available in Zarr on S3, or requires conversion.

---

## Out of scope (this pass)

- `data.json` endpoint
- Authentication / rate limiting
- Zarr migration
