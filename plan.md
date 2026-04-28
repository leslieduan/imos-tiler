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
  main.py                    ← add tiles router (small change)
  constants.py               ← Product dataclass + all 5 product configs
  routers/
    tiles.py                 ← 2 endpoints: tile PNG + manifest
  services/
    loader.py                ← S3 open → xr.Dataset → time-select → LRU cache
    renderer.py              ← pure rendering: resample + chunk extract + PNG encode
```

---

## Steps

### 1. Dependencies (`pyproject.toml`)

Add: `xarray`, `s3fs`, `Pillow`, `scipy`, `netcdf4`, `cachetools`

### 2. `constants.py`

Extend `Product` dataclass with a `coord_names: dict` field (default empty), then define all five product configs.

Products:
- `SST_ANOM_MOSAIC` — LODs 1/2/3, variable `sst_anom_mosaic`, 24-bit scalar encoding
- `MARINE_HEATWAVE_DHD_MOSAIC` — LODs 1/2/3, variable `dhd_mosaic`, 24-bit scalar encoding
- `MARINE_HEATWAVE_SSTA_MOSAIC` — LODs 1/2/3, variable `ssta_mosaic`, 24-bit scalar encoding
- `OCEAN_CURRENT` — LOD 1, variables `[UCUR, VCUR]`, UV 8-bit encoding, `coord_names={"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}`
- `SEA_LEVEL_ANOMALY` — LOD 1, variable `GSLA`, 24-bit scalar encoding, `coord_names={"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}`

### 3. `services/loader.py`

```python
load_dataset(product_id: str, date: str) -> xr.Dataset
```

- Opens NetCDF from S3 via `s3fs.S3FileSystem(anon=True)`
- If `product.coord_names` is set, renames coordinates immediately after open: `ds.rename(product.coord_names)` — all downstream code sees `time`/`lat`/`lon` uniformly
- Time-selects using `.sel(time=date)` or `.isel(time=0)` depending on product (MHW uses isel)
- Caches result in `cachetools.LRUCache` keyed on `(product_id, date)` — avoids repeated S3 loads for the same date when multiple tiles are requested

### 4. `services/renderer.py`

Two public functions:

```python
render_tile(product: Product, ds: xr.Dataset, lod: int, cx: int, cy: int) -> bytes
render_manifest(product: Product, ds: xr.Dataset) -> dict
```

`render_tile` ports the following from the chunking scripts:
- `_resample_to_grid` — `ds.interp()` to `(total_w × total_h)` with north→south latitude
- `_extract_chunk` — slice with 1-pixel padding + edge replication
- PNG encoding — 24-bit scalar path (SSTA/MHW/SLA) or UV path (ocean current)
- Returns raw PNG bytes via `PIL.Image.save()` to a `BytesIO` buffer with `optimize=False`

`render_manifest` ports `_to_manifest` from each chunking script, returning a dict (no file I/O).

### 5. `routers/tiles.py`

```python
GET /tiles/{product_id}/{date}/{z}/{x}/{y}.png
```
1. Look up product by `product_id` → 404 if not found
2. Validate `z` is in `product.lod_grids` → 404 if not
3. Validate `x` < `grid_cols` and `y` < `grid_rows` for that LOD → 404 if out of bounds
4. `ds = load_dataset(product_id, date)` — may raise `FileNotFoundError` → 404
5. `png_bytes = render_tile(product, ds, z, x, y)`
6. Return `Response(content=png_bytes, media_type="image/png")`

```python
GET /tiles/{product_id}/{date}/manifest.json
```
1. Look up product → 404
2. `ds = load_dataset(product_id, date)` → 404 on miss
3. `manifest = render_manifest(product, ds)`
4. Return `JSONResponse(content=manifest)`

### 6. `main.py`

```python
from routers.tiles import router as tiles_router
app.include_router(tiles_router, prefix="/tiles", tags=["Tiles"])
```

---

## Out of scope (this pass)

- `data.json` endpoint
- Disk-based tile caching / pre-warming
- Authentication / rate limiting
