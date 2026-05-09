# Technical Reference

## Background: Why Zarr

The NetCDF/HDF5 stack had an unacceptable cold-start cost for cloud-native serving. HDF5 B-tree traversal requires hundreds of sequential HTTP round-trips regardless of what code does — it is a file format constraint, not fixable in the application layer. Observed cold starts from home internet: ssta ~30s, Marine Heatwave 90s+ (8m 34s TTFB measured). Even in-region on AWS, Marine Heatwave takes 2–4s on cold start due to its 15 variables × 7.8M pixel grid.

Zarr eliminates this entirely: metadata is one `.zmetadata` HTTP request, and variable chunks are directly addressable with no traversal. The NetCDF stack has been removed.

**Full format analysis and IMOS product file details: `docs/netcdf-vs-zarr.md`.**

### Active products

| Product               | Variable(s) | Zarr store                                              |
| --------------------- | ----------- | ------------------------------------------------------- |
| Sea level anomaly     | GSLA        | `model_sea_level_anomaly_gridded_realtime.zarr`         |
| Ocean current         | UCUR, VCUR  | `model_sea_level_anomaly_gridded_realtime.zarr`         |
| Radar wind (SA Gulfs) | WDIR        | `radar_SouthAustraliaGulfs_wind_delayed_qc.zarr`        |
| AusTemp heatwave SSTA | ssta        | `satellite_austemp_heatwave_8day.zarr`                  |

---

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │           Frontend (WebGL)            │
                    └──────────────────┬───────────────────┘
                                       │ HTTP
                    ┌──────────────────▼───────────────────┐
                    │            FastAPI  main.py           │
                    └──────────────────┬────────────────────┘
                                       │
                          ┌────────────▼────────────┐
                          │       routers/tiles.py   │
                          │          /tiles          │
                          └──────┬──────────┬────────┘
                                 │          │
                    ┌────────────▼──────┐ ┌─▼──────────────────┐
                    │  services/        │ │  services/          │
                    │  loader.py        │ │  renderer.py        │
                    │                   │ │                     │
                    │  store singleton  │ │  processed grid     │
                    │  slice cache      │ │  cache LRU(20)      │
                    │  LRU(20)          │ │  (id(ds), lod)      │
                    │  (url,date,vars)  │ │                     │
                    └────────┬──────────┘ └─────────────────────┘
                             │ miss
                    ┌────────▼────────┐
                    │     AWS S3      │
                    │   Zarr stores   │
                    └─────────────────┘
```

**Request flow**

```
cold  → get_lod_grids (opens store, computes, writes product.lod_grids) → load_slice (.compute()) → _get_processed (resample) → _extract_chunk → PNG encode
warm  → get_lod_grids (product.lod_grids already set)                   → load_slice (cache hit)  → _get_processed (cache hit) → _extract_chunk → PNG encode
```

---

## URL contract

```
GET /tiles/{product_id}/{date}/{z}/{x}/{y}.png     → RGBA PNG tile
GET /tiles/{product_id}/{date}/manifest.json       → bounds + value ranges + LOD grid config
GET /tiles/{product_id}/{date}/point?lat=&lon=     → variable value at point
```

`z` = LOD level, `x` = chunk column (0 = westernmost), `y` = chunk row (0 = northernmost). Not Web Mercator — custom atlas grid in geographic (lat/lon) space.

---

## File structure

```
titiler-project/
  main.py                        ← single router wired up, CORS middleware, titiler COG router
  constants.py                   ← Product dataclass + LOD algorithm; 4 active products
                                    LOD_ZOOM_THRESHOLDS, DEFAULT_LOD_GRIDS, MAX_LODS, MIN_COARSEST_GRID
  docs/
    technical.md                 ← this file
    dataset.md                   ← per-store variable/dimension/chunking reference
    netcdf-vs-zarr.md            ← format comparison, IMOS product file analysis, performance data
  routers/
    tiles.py                     ← /tiles
  services/
    loader.py                    ← Zarr store singleton + per-(date, variables) slice cache + get_lod_grids
    renderer.py                  ← processed grid cache + chunk extract + PNG encode
```

---

## LOD grid system

### Constants (`constants.py`)

- `MAX_LODS = 4` — frontend WebGL atlas limit: at most 4 LOD levels per product
- `MIN_COARSEST_GRID = (2, 2)` — minimum (cols, rows) for the coarsest LOD level; levels below this are dropped. If all levels are filtered out (data smaller than one chunk), falls back to the native finest grid so there is always at least one LOD.
- `LOD_ZOOM_THRESHOLDS: dict[int, int]` — universal map zoom thresholds applied to all products (e.g. `{2: 4, 3: 5, 4: 6}`)

### Algorithm (`Product._compute_lod_grids` in `constants.py`)

Derives LOD grids from actual data dimensions and chunk size. Accepts `max_lods` and `min_coarsest` as parameters (defaulting to the constants above).

1. Finest level: `ceil(data_width / chunk_w) × ceil(data_height / chunk_h)`
2. Depth: `floor(log2(max(finest_cols, finest_rows)))` — number of halvings before both axes reach 1 (uses `max` so elongated grids go as deep as the wider axis allows)
3. Each level `k`: `(ceil(finest_cols / 2^k), ceil(finest_rows / 2^k))` — `ceil` preserves coverage at intermediate scales (e.g. `finest=5` → `3, 2` not `2, 1`)
4. Drop levels whose cols or rows fall below `min_coarsest`. If nothing remains (data fits within a single chunk), fall back to `(finest_cols, finest_rows)` directly.
5. Take the finest `max_lods` levels; assign LOD indices starting at 1 (coarsest)

Example: `Product._compute_lod_grids(3000, 1500, (256, 256))` → `{1: (3, 2), 2: (6, 3), 3: (12, 6)}`

Small dataset example (radar SA Gulfs, 102×74, chunk 240×192): finest=(1,1), filtered to nothing, fallback → `{1: (1, 1)}`

### Lazy population (`services/loader.py` — `get_lod_grids`)

Products are defined in `constants.py` with `lod_grids={}`. On the first request:

1. `get_lod_grids(product)` checks `product.lod_grids` — empty, so proceeds
2. Opens the Zarr store (singleton — reused across all calls to the same URL)
3. Reads lat/lon dimension sizes from store metadata (`.zmetadata`, no data fetch)
4. Calls `Product._compute_lod_grids` and writes the result back via `object.__setattr__` (bypasses `frozen=True` for this one field only)
5. All subsequent calls return immediately from the `if product.lod_grids` guard

---

## Coordinate normalisation

On store open, `_get_store` applies `COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}` to rename any uppercase coordinate names to lowercase. This happens once per store URL and is stored in the singleton. All downstream code (renderer, manifest, point endpoint) can always assume `lat`/`lon`/`time` regardless of what the store uses natively.

---

## Caching strategy

All caches are in-memory LRU (cachetools), evicted least-recently-used. Nothing written to disk.

**Layer 1 — Store singleton** (`services/loader.py`, `_stores` dict keyed by URL)

Caches the open Zarr store handle (lazy, metadata only). One HTTP request per store URL ever. Shared across all products using the same store.

**Layer 2 — Slice cache** (`services/loader.py`, keyed `(store_url, date, variables)`, maxsize=20)

Stores a fully-computed (`.compute()`) 2D lat×lon numpy slice. This is the only S3 data read — one chunk fetch per cold (date, variable) pair. Keyed by `variables` so different products using the same store cache independently.

**Layer 3 — Processed grid cache** (`services/renderer.py`, keyed `(id(ds), lod)`, maxsize=20)

Stores the resampled + normalised numpy arrays for the full LOD grid. A hit reduces per-tile work to `_extract_chunk` + PNG encode only — no S3 I/O, no resampling. `id(ds)` is stable because `ds` is held alive by Layer 2.

### Thread safety

All caches use `threading.Lock`. The processed grid cache additionally uses a `threading.Event` per in-flight key: concurrent requests for the same `(ds, lod)` wait for the first computation to complete rather than duplicating it.

---

## Adding a new product

Products are managed at runtime via the admin API — no code changes or redeploy required. All products are persisted in `products.json` (the single source of truth) and loaded into memory on startup.

### Via admin API (runtime)

```bash
# Scalar variable
curl -X POST http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"id": "my_product", "source_path": "s3://my-bucket/my_product.zarr", "variable": "VAR_NAME"}'

# UV (vector) product
curl -X POST http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"id": "my_uv_product", "source_path": "s3://my-bucket/my_product.zarr", "variable": ["U_VAR", "V_VAR"]}'
```

On the first request after registration:
- The store is opened and coordinates are normalised automatically
- LOD grids are computed from the store's actual lat/lon dimensions
- Rendering and manifest generation work generically from `product.variable`

### Requirements for the Zarr store

| Requirement | Detail |
|---|---|
| Coordinate names | Must be `lat`/`lon`/`time`, or the uppercase variants `LATITUDE`/`LONGITUDE`/`TIME` (renamed automatically on open). If a store uses different names, add a mapping to `COORD_NAMES` in `constants.py`. |
| Spatial dimensions | `lat` and `lon` must be present after normalisation — `_get_store` raises `ValueError` with a clear message if not. |
| Variable | The variable(s) named in `Product.variable` must exist in the store. |

### Optional overrides

`Product` fields can be customised per product if the defaults don't fit:

| Field | Default | When to override |
|---|---|---|
| `chunk_px` | `(240, 192)` | Store has very small or very large spatial extent |
| `padding` | `1` | Tile edge artefacts, or no padding needed |
| `lod_grids` | `{}` (auto-computed) | Pre-set known grids to skip the first-request computation |

---

## Coordinate system and projection pipeline

### Server — Plate Carrée (EPSG:4326)

Tiles are produced in **Plate Carrée** (equirectangular projection) — longitude maps linearly to pixel X, latitude maps linearly to pixel Y. This is the visual representation of EPSG:4326 / WGS84 geographic coordinates.

The projection is implemented implicitly in `_resample_to_grid` (`services/renderer.py`):

```python
target_lons = np.linspace(lon_min, lon_max, total_w)  # lon → x (linear)
target_lats = np.linspace(lat_max, lat_min, total_h)  # lat → y (linear, north→south)
```

`np.linspace` distributes points evenly in degrees — that linear mapping **is** Plate Carrée. No projection formula is needed. Tiles are essentially slices of the native lat/lon data grid with no reprojection.

The manifest returns bounds in geographic degrees (`lonMin`, `lonMax`, `latMin`, `latMax`), not projected metres.

Plate Carrée is the right choice here because:
- Source Zarr data is on a regular lat/lon grid — tiles map directly with no reprojection overhead
- Scientific accuracy is preserved — distances at different latitudes are not distorted
- Standard for oceanographic datasets (IMOS, ERA5, CMIP6 all use regular lat/lon grids)

### Frontend — Web Mercator base map

The frontend map canvas is in **Web Mercator (EPSG:3857)**. The WebGL shader converts each fragment's Mercator position to lon/lat (inverse Mercator formula), then samples the Plate Carrée tile using a linear lat/lon lookup — matching the server's `np.linspace` mapping. Data value decoding and colour ramp application happen in the same pass.

### Manifest as the contract between server and shader

The manifest is the interface between the server's coordinate system and the shader's uniforms:

| Manifest field | Shader uniform | Purpose |
|---|---|---|
| `bounds.lonMin/lonMax/latMin/latMax` | `u_data_bounds` | geographic extent for tile sampling |
| `lods[n].grid` | `u_lod_grids` | cols×rows per LOD for chunk lookup |
| `valueRange` | `u_value_range` | decode uint24 back to raw value |
| `lods[n].chunkPx` / `storedPx` / `padding` | `u_uv_scale`, `u_uv_offset` | skip padding border in atlas UV |

---

## PNG encoding contract

Tiles are RGBA PNGs (`optimize=False`). The byte layout is fixed and consumed by a WebGL shader:

- **24-bit scalar** (GSLA, SSTA, DHD, SLA, WDIR): R=high byte, G=mid byte, B=low byte of normalised uint24; A=ocean mask (255=ocean, 0=land, premultiplied).
- **Particle / vector** (UV — e.g. ocean current, wind): R=U normalised to 8-bit, G=V normalised to 8-bit, B=ocean mask×255, A=255.

Normalisation ranges (`val_min`/`val_max`, `u_min`/`u_max`, etc.) are computed from the full pre-resampled dataset and returned in `manifest.json`. All tiles for a date share the same ranges.

---
