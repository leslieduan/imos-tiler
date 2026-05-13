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
                    │  slice cache      │ │  cache LRU          │
                    │  LRU              │ │  (id(ds), lod)      │
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

The two tile APIs use `z`/`x`/`y` with fundamentally different coordinate systems — see [`docs/tile_system.md`](tile_system.md) for a focused explanation.

### Data tiles (`/data_tiles`)

Raw value-encoded RGBA tiles for WebGL shader consumption. Uses a custom geographic atlas grid — **not** Web Mercator.

```
GET /data_tiles/products                                 → list all registered products
GET /data_tiles/manifest?from=YYYY-MM-DD&to=YYYY-MM-DD  → available dates for all products
GET /data_tiles/{product_id}/{date}/{z}/{x}/{y}.png      → raw RGBA PNG tile
GET /data_tiles/{product_id}/{date}/manifest.json        → bounds + value ranges + LOD grid config
GET /data_tiles/{product_id}/{date}/point?lat=&lon=      → variable value at point
```

`z` = LOD level, `x` = chunk column (0 = westernmost), `y` = chunk row (0 = northernmost). Not Web Mercator — custom atlas grid in geographic (lat/lon) space.

### Visual tiles (`/visual_tiles`)

Colourised PNG tiles in standard Web Mercator (XYZ) — compatible with MapboxGL `raster` sources. Single-variable products only.

```
GET /visual_tiles/colormaps                                                        → all supported colormap names
GET /visual_tiles/{product_id}/{date}/{z}/{x}/{y}.png?colormap=viridis&rescale=min,max
```

| Query param | Default | Description |
|---|---|---|
| `colormap` | `viridis` | Colormap name — rio-tiler built-in, matplotlib name, or custom registered name |
| `rescale` | data min/max for the date | Value range as `min,max`, e.g. `-0.5,0.5` |

`z`/`x`/`y` here are standard Web Mercator tile coordinates (as used by OpenStreetMap, MapboxGL, Leaflet, etc.).

### `/tiles/manifest` — products availability

Returns available dates for every registered product, filtered by an optional date range.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `from` | 3 months before today | Start date inclusive (YYYY-MM-DD) |
| `to` | unbounded | End date inclusive (YYYY-MM-DD) |

```json
{
  "products": {
    "sea_level_anomaly": { "available_dates": ["2024-02-01", "2024-02-02", ...] },
    "ocean_current":     { "available_dates": ["2024-02-01", ...] }
  }
}
```

**Performance**: dates are read from the `time` coordinate of each Zarr store — a 1D array held in the store singleton. No spatial data chunks are touched. Filtering is an in-memory string comparison (ISO dates sort lexicographically). Responses are always sub-millisecond: the store is pre-warmed at startup and refreshed in the background after TTL expiry, so no request ever waits for a re-open.

---

## File structure

```
titiler-project/
  main.py                        ← mounts all routers, CORS middleware, lifespan startup
  constants.py                   ← Product dataclass + LOD algorithm; CUSTOM_COLORMAPS registry
                                    LOD_ZOOM_THRESHOLDS, MAX_LODS, MIN_COARSEST_GRID
  products.json                  ← persisted product registrations (runtime, gitignored)
  colormaps.json                 ← persisted custom colormap registrations (runtime, gitignored)
  docs/
    technical.md                 ← this file
    dataset.md                   ← per-store variable/dimension/chunking reference
    netcdf-vs-zarr.md            ← format comparison, IMOS product file analysis, performance data
  routers/
    data_tiles.py                ← /data_tiles — raw value-encoded RGBA tiles for WebGL
    visual_tiles.py              ← /visual_tiles — colourised Web Mercator XYZ tiles
    products.py                  ← GET /products, manifest, and point endpoints — included by both tile routers
    admin.py                     ← /admin — product and colormap management (key-protected)
  services/
    loader.py                    ← Zarr store singleton + per-(date, variables) slice cache + get_lod_grids
    renderer.py                  ← processed grid cache + chunk extract + PNG encode (data tiles)
    visual_renderer.py           ← Web Mercator tile render + colormap lookup (visual tiles)
    product_store.py             ← products.json read/write + in-memory PRODUCTS dict management
    colormap_store.py            ← colormaps.json read/write + in-memory CUSTOM_COLORMAPS management
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

## Date and timezone convention

**This is a critical invariant.** Getting it wrong causes silent 404s or data served for the wrong day.

### The rule

| Layer | Representation |
|---|---|
| Zarr store `time` coordinate | UTC — numpy `datetime64[ns]` is always UTC by convention |
| API request/response dates | Local Australian time — `Australia/Sydney` (AEST UTC+10 / AEDT UTC+11) |

All satellite passes over Australia occur during Australian daytime. Their UTC timestamps typically fall on the **previous UTC day** (e.g. a pass at `2022-06-01 01:20 AEST` is `2022-05-31 15:20 UTC`). Comparing UTC dates to local request dates directly would return a 404 for every such record.

### How the server handles it

Every point where a UTC timestamp is exposed or compared is converted to local time via `_ts_to_local_date` in `services/loader.py`:

```python
_LOCAL_TZ = ZoneInfo("Australia/Sydney")  # handles AEST/AEDT automatically

def _ts_to_local_date(ts) -> str:
    return pd.Timestamp(ts).tz_localize("UTC").tz_convert(_LOCAL_TZ).strftime("%Y-%m-%d")
```

- **`get_available_dates`** — returns local dates so the frontend always receives values it can round-trip back as request dates.
- **`load_slice`** — converts the requested local date to local midnight, then to UTC, before calling `sel(time=..., method="nearest")`. After selection, the returned timestamp is converted back to a local date and compared to the requested date to guard against `method="nearest"` reaching too far.

### What to check when adding a new product

If you add a product whose Zarr store timestamps are stored differently (e.g. already in local time, or in a different timezone), you must either adjust `_ts_to_local_date` or override the date handling for that product. Do not change the default without understanding this invariant first.

---

## Coordinate normalisation

On store open, `_get_store` applies `COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}` to rename any uppercase coordinate names to lowercase. This happens once per store URL and is stored in the singleton. All downstream code (renderer, manifest, point endpoint) can always assume `lat`/`lon`/`time` regardless of what the store uses natively.

---

## Concurrency

See [`docs/concurrency.md`](concurrency.md) for the full concurrency model, capacity evaluation, stampede protection, and scaling notes.

The three sizing env vars form a consistent chain — if you raise `THREAD_POOL_SIZE`, raise the other two proportionally:

```
THREAD_POOL_SIZE=100  →  SLICE_CACHE_SIZE=100  →  PROCESSED_CACHE_SIZE=400
(max concurrent cold       (retain everything          (SLICE_CACHE_SIZE × ~4 LOD levels)
 requests)                  that gets computed)
```

---

## Caching strategy

All caches are in-memory LRU (cachetools), evicted least-recently-used. Nothing written to disk.

**Layer 1 — Store singleton** (`services/loader.py`, `_stores` dict keyed by URL)

Caches the open Zarr store handle (lazy, metadata only). Shared across all products using the same store.

Uses a **stale-while-revalidate** strategy to pick up newly appended time steps without ever blocking a request:

- **Startup** — `prewarm_stores` in `main.py` opens every registered store in background daemon threads so the cache is warm before the first request arrives.
- **Within TTL** — the cached store is returned immediately (sub-millisecond).
- **After TTL** (`STORE_TTL_SECONDS`, default `600`) — the stale store is returned immediately for the current request, and a single background daemon thread calls `_refresh_store_background` to re-open the store. `_store_refreshing` prevents duplicate refresh threads for the same URL.
- **First-ever open** (no cached entry) — the request blocks until `xr.open_zarr` completes; all concurrent requests for the same URL wait on the same `Future` rather than each opening independently.

Re-opening is cheap — `xr.open_zarr` reads only metadata and coordinate arrays (`time`, `lat`, `lon`), no data chunks. In-flight `load_slice` calls hold a direct Python reference to the old dataset object and complete normally. `_slice_cache` and `_processed_cache` entries for existing dates remain valid and unaffected.

**Layer 2 — Slice cache** (`services/loader.py`, keyed `(store_url, date, variables)`)

Stores a fully-computed (`.compute()`) 2D lat×lon numpy slice. This is the only S3 data read — one chunk fetch per cold (date, variable) pair. Keyed by `variables` so different products using the same store cache independently.

Size is controlled by the `SLICE_CACHE_SIZE` env var (default `100`). Each entry is roughly 2–7 MB depending on grid size and number of variables. Should be kept at least equal to `THREAD_POOL_SIZE` so a burst of cold requests does not immediately evict freshly computed slices.

**Layer 3 — Processed grid cache** (`services/renderer.py`, keyed `(id(ds), lod)`)

Stores the resampled + normalised numpy arrays for the full LOD grid. A hit reduces per-tile work to `_extract_chunk` + PNG encode only — no S3 I/O, no resampling. `id(ds)` is stable because `ds` is held alive by Layer 2.

Size is controlled by the `PROCESSED_CACHE_SIZE` env var (default `400`). Should be set to at least `SLICE_CACHE_SIZE × number_of_LOD_levels` so every cached slice can have its processed grids cached too.

### Thread safety

All caches use `threading.Lock`. The processed grid cache additionally uses a `threading.Event` per in-flight key: concurrent requests for the same `(ds, lod)` wait for the first computation to complete rather than duplicating it.

---

## Colormap system

Visual tiles support any colormap name that resolves through the following lookup chain (first match wins):

1. **Custom registry** (`CUSTOM_COLORMAPS` in `constants.py` / `colormaps.json`) — names registered at compile time or via the admin API.
2. **rio-tiler built-ins** — e.g. `viridis`, `plasma`, `inferno`.
3. **matplotlib** — any name from `matplotlib.colormaps`, including diverging maps like `RdBu_r`, `coolwarm`.

An unrecognised name returns `400 Bad Request`.

### Listing supported colormaps

`GET /visual_tiles/colormaps` returns all supported names grouped by source, with higher-priority sources excluding duplicate names from lower ones:

```json
{
  "custom":    ["blue_red", "imos_sst"],
  "rio_tiler": ["accent", "algae", "viridis", ...],
  "matplotlib": ["Blues", "RdBu_r", "coolwarm", ...]
}
```

### Custom colormaps

Each custom colormap is a list of exactly 256 RGBA tuples — one per normalised byte value (0 = data minimum, 255 = data maximum after rescaling).

**Compile-time defaults** live in `CUSTOM_COLORMAPS` in `constants.py` and are always available:

```python
CUSTOM_COLORMAPS: dict[str, list[tuple[int, int, int, int]]] = {
    "blue_red": [(i, 0, 255 - i, 255) for i in range(256)],
}
```

**Runtime additions** are persisted in `colormaps.json` and managed via the admin API (`POST /admin/colormaps`, `DELETE /admin/colormaps/{name}`). They are loaded on startup by `load_colormaps()` in `services/colormap_store.py` and take effect immediately without a server restart.

### Cache behaviour

`_colormap()` in `services/visual_renderer.py` is `@lru_cache`-d (max 64 entries). The cache is cleared automatically whenever a colormap is added, updated, or deleted via the admin API. Compile-time defaults (from `constants.py`) are never cleared.

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
