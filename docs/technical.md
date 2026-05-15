# Technical Reference

## Background: Why Zarr

The NetCDF/HDF5 stack had an unacceptable cold-start cost for cloud-native serving. HDF5 B-tree traversal requires hundreds of sequential HTTP round-trips regardless of what code does — it is a file format constraint, not fixable in the application layer. Observed cold starts from home internet: ssta ~30s, Marine Heatwave 90s+ (8m 34s TTFB measured). Even in-region on AWS, Marine Heatwave takes 2–4s on cold start due to its 15 variables × 7.8M pixel grid.

Zarr eliminates this entirely: metadata is one `.zmetadata` HTTP request, and variable chunks are directly addressable with no traversal. The NetCDF stack has been removed.

**Full format analysis and IMOS product file details: `docs/netcdf-vs-zarr.md`.**

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│                         Client  (WebGL / Map)                              │
└────────────────────────────────────────────────────────────────────────────┘
                                      │ HTTP
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                          Nginx  (reverse proxy)                            │
└────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                            FastAPI  (main.py)                              │
│                                                                            │
└──────────────────┬───────────────────────────────────────┬─────────────────┘
                   │                                       │
                   ▼                                       ▼
┌────────────────────────────────────┐  ┌────────────────────────────────────┐
│          Tile Routers              │  │             /admin                 │
│  /data_tiles  ·  /visual_tiles     │  │           admin.py                 │
│       products.py  (shared)        │  │          X-Admin-Key               │
│  /products · /manifest · /point    │  │                                    │
└──────────────────┬─────────────────┘  └────────────────────────────────────┘
                   │
                   ├───────────────────────────────────────┐
                   │                                       │
                   ▼                                       ▼
┌────────────────────────────────────┐  ┌────────────────────────────────────┐
│        data_renderer.py            │  │       visual_renderer.py           │
│                                    │  │                                    │
│   L1 Processed grid cache          │  │   XarrayReader                     │
│   (data tiles only)                │  │   Web Mercator reprojection        │
│                                    │  │   no L1 cache                      │
└──────────────────┬─────────────────┘  └──────────────────┬─────────────────┘
                   │ L1 miss                               │ every request
                   └──────────────────┬────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                              loader.py                                     │
│                                                                            │
│   Store singleton                    L2 Slice cache                        │
│   one per Zarr URL                   (url, date, vars)                     │
│   stale-while-revalidate             fully-computed 2D lat×lon slice       │
└────────────────────────────────────────────────────────────────────────────┘
                                      │ L2 miss
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                           L3  Disk cache                                   │
│                   .pkl.lz4 per date  ·  DISK_CACHE_PATH                    │
└────────────────────────────────────────────────────────────────────────────┘
                                      │ L3 miss
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                              AWS  S3                                       │
│                            Zarr stores                                     │
└────────────────────────────────────────────────────────────────────────────┘
```

**Background tasks** run concurrently on the event loop — never blocking request handling:

```
server start  ──► prewarm_stores          warm all store singletons before first request
server start  ──► prewarm_disk_slices     fill L3 for latest CACHE_DAYS dates (ThreadPoolExecutor)
every 4 hours ──► _cache_refresh_loop     add newly available dates, evict stale from L3
product added ──► prewarm_disk_slices     triggered async by POST /admin/products
```

### Request flow

**Data tiles** (`/data_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png`):

`load_slice` is lazy — it is only called when `_get_processed` misses. On a processed cache hit, no slice I/O occurs at all.

```
processed warm → get_lod_grids (already set) → _get_processed (cache hit)                            → _extract_chunk → PNG encode
disk warm      → get_lod_grids (already set) → _get_processed miss → load_slice (disk read, ~30ms)   → resample → cache → _extract_chunk → PNG encode
S3 cold        → get_lod_grids (already set) → _get_processed miss → load_slice (S3 .compute(), ~2s) → resample → cache → _extract_chunk → PNG encode
```

**Visual tiles** (`/visual_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png` or `/bbox`):

```
S3 cold   → load_slice (S3 .compute(), ~2s) → _to_scalar_parts (antimeridian split if needed) → XarrayReader.tile/part → colormap + PNG encode
disk warm → load_slice (disk read, ~30ms)    → _to_scalar_parts → XarrayReader.tile/part → colormap + PNG encode
mem warm  → load_slice (L2 hit, <1ms)        → _to_scalar_parts → XarrayReader.tile/part → colormap + PNG encode
```

Visual tiles use `rio-tiler`'s `XarrayReader` for Web Mercator reprojection; no processed grid cache is involved.

---

## File structure

```
titiler-project/
  main.py                        ← mounts all routers, CORS middleware, lifespan startup
  constants.py                   ← Product dataclass + LOD algorithm
                                    LOD_ZOOM_THRESHOLDS, MAX_LODS, MIN_COARSEST_GRID
  products.json                  ← persisted product registrations (runtime, gitignored; local dev default)
  colormaps.json                 ← persisted custom colormap registrations (runtime, gitignored; local dev default)
  data/
    products.json                ← Docker: mounted volume, set via PRODUCTS_CONFIG_PATH=data/products.json
    colormaps.json               ← Docker: mounted volume, set via COLORMAPS_CONFIG_PATH=data/colormaps.json
  docs/
    technical.md                 ← this file
    dataset.md                   ← per-store variable/dimension/chunking reference
    netcdf-vs-zarr.md            ← format comparison, IMOS product file analysis, performance data
  routers/
    data_tiles.py                ← /data_tiles — raw value-encoded RGBA tiles for WebGL
    visual_tiles.py              ← /visual_tiles — colourised Web Mercator XYZ tiles + bbox
    products.py                  ← shared: /products, /manifest, /{id}/{date}/point — included by both tile routers
    admin.py                     ← /admin — product and colormap management (key-protected)
  services/
    loader.py                    ← Zarr store singleton + L2 slice cache + disk cache + get_lod_grids
    data_renderer.py             ← processed grid cache + chunk extract + PNG encode (data tiles)
    visual_renderer.py           ← Web Mercator tile render + bbox render + colormap lookup (visual tiles)
    product_store.py             ← products.json read/write + in-memory PRODUCTS dict management
    colormap_store.py            ← colormaps.json read/write + in-memory colormap registry + ColormapMode type
```

`products.json` and `colormaps.json` default to the project root in local dev. In Docker (`docker-compose.yml`), they are overridden to `data/products.json` and `data/colormaps.json`, backed by a `./data` host volume. The disk slice cache directory is set via `DISK_CACHE_PATH` (default: unset in local dev; `/app/slice_cache` in Docker, backed by a `./slice_cache` host volume).

---

## URL contract

The two tile APIs use `z`/`x`/`y` with fundamentally different coordinate systems — see [`docs/tile_system.md`](tile_system.md) for a focused explanation.

### Shared endpoints (available under both `/data_tiles` and `/visual_tiles`)

`routers/products.py` is included by both tile routers, so these paths exist under both prefixes:

```
GET /{prefix}/products                                   → list all registered products
GET /{prefix}/manifest?from=YYYY-MM-DD&to=YYYY-MM-DD    → available dates for all products
GET /{prefix}/{product_id}/{date}/point?lat=&lon=        → variable value at point
```

`/manifest` parameters:

| Parameter | Default               | Description                       |
| --------- | --------------------- | --------------------------------- |
| `from`    | 3 months before today | Start date inclusive (YYYY-MM-DD) |
| `to`      | unbounded             | End date inclusive (YYYY-MM-DD)   |

```json
{
  "products": {
    "sea_level_anomaly": { "available_dates": ["2024-02-01", "2024-02-02", ...] },
    "ocean_current":     { "available_dates": ["2024-02-01", ...] }
  }
}
```

**Performance**: dates are read from the `time` coordinate of each Zarr store — a 1D array held in the store singleton. No spatial data chunks are touched. Filtering is an in-memory string comparison. Responses are always sub-millisecond once the store is warm.

### Data tiles (`/data_tiles`)

Raw value-encoded RGBA tiles for WebGL shader consumption. Uses a custom geographic atlas grid — **not** Web Mercator.

```
GET /data_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png → raw RGBA PNG tile
GET /data_tiles/{product_id}/{date}/manifest.json         → bounds + value ranges + LOD grid config
```

`z` = LOD level, `x` = chunk column (0 = westernmost), `y` = chunk row (0 = northernmost).

### Visual tiles (`/visual_tiles`)

Colourised PNG tiles in standard Web Mercator (XYZ) — compatible with MapboxGL `raster` sources. Single-variable products only.

```
GET /visual_tiles/colormaps                                            → all supported colormap names
GET /visual_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png           → colourised Web Mercator PNG
GET /visual_tiles/{product_id}/{date}/bbox?bbox=minx,miny,maxx,maxy  → colourised PNG for arbitrary bbox
```

`z`/`x`/`y` are standard Web Mercator tile coordinates (OpenStreetMap, MapboxGL, Leaflet, etc.).

Visual tile query parameters:

| Query param | Default                   | Description                                                                    |
| ----------- | ------------------------- | ------------------------------------------------------------------------------ |
| `colormap`  | `viridis`                 | Colormap name — rio-tiler built-in, matplotlib name, or custom registered name |
| `rescale`   | data min/max for the date | Value range as `min,max`, e.g. `-0.5,0.5`                                      |

Bbox-specific query parameters:

| Query param | Default     | Description                                                                                                                      |
| ----------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `bbox`      | required    | Bounding box as `minx,miny,maxx,maxy` in the CRS specified by `crs`                                                              |
| `width`     | `256`       | Output image width in pixels (1–2048)                                                                                            |
| `height`    | `256`       | Output image height in pixels (1–2048)                                                                                           |
| `crs`       | `EPSG:4326` | CRS of the bbox coordinates. `EPSG:4326` for geographic degrees; `EPSG:3857` for Web Mercator meters (Mapbox `{bbox-epsg-3857}`) |

### Admin API (`/admin`)

All endpoints require the `X-Admin-Key` header.

```
POST   /admin/products              → register a new product
DELETE /admin/products/{product_id} → remove a product

POST   /admin/colormaps             → register a custom colormap
DELETE /admin/colormaps/{name}      → remove a custom colormap
```

---

## Active products

Products are runtime-managed: `PRODUCTS` in `constants.py` starts empty and is populated on startup from `products.json` (written by the admin API). The table below reflects the products registered in the default deployment.

| Product ID                                       | Variable(s) | Zarr store                                       |
| ------------------------------------------------ | ----------- | ------------------------------------------------ |
| `sea_level_anomaly`                              | GSLA        | `model_sea_level_anomaly_gridded_realtime.zarr`  |
| `ocean_current`                                  | UCUR, VCUR  | `model_sea_level_anomaly_gridded_realtime.zarr`  |
| `radar_SouthAustraliaGulfs_wind_delayed_qc_wdir` | WDIR        | `radar_SouthAustraliaGulfs_wind_delayed_qc.zarr` |
| `satellite_austemp_heatwave_8day_ssta`           | ssta        | `satellite_austemp_heatwave_8day.zarr`           |

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

Products start with `lod_grids={}`. On the first request:

1. `get_lod_grids(product)` checks `product.lod_grids` — empty, so proceeds (double-checked locking)
2. Opens the Zarr store (singleton — reused across all calls to the same URL)
3. Reads lat/lon dimension sizes from store metadata (`.zmetadata`, no data fetch)
4. Calls `product.apply_computed_lod_grids(data_width, data_height)`, which runs `_compute_lod_grids` and populates the result via `self.lod_grids.update()`. Although `Product` is a frozen dataclass, `lod_grids` is a mutable dict — `update()` mutates the dict in place without reassigning the attribute, so no frozen bypass is needed.
5. All subsequent calls return immediately from the `if product.lod_grids` guard

---

## Date and timezone convention

**This is a critical invariant.** Getting it wrong causes silent 404s or data served for the wrong day.

### The rule

| Layer                        | Representation                                                                         |
| ---------------------------- | -------------------------------------------------------------------------------------- |
| Zarr store `time` coordinate | UTC — numpy `datetime64[ns]` is always UTC by convention                               |
| API request/response dates   | Local time in `TILE_TIMEZONE` (default `Australia/Sydney`, AEST UTC+10 / AEDT UTC+11) |

`TILE_TIMEZONE` is an IANA timezone name read at startup from the environment variable of the same name. To deploy this server for a different region, set it in `.env` or `docker-compose.yml` before starting — no code changes are needed. All date conversion (manifest output, tile request matching, error messages) uses the configured timezone automatically.

All satellite passes over Australia occur during Australian daytime. Their UTC timestamps typically fall on the **previous UTC day** (e.g. a pass at `2022-06-01 01:20 AEST` is `2022-05-31 15:20 UTC`). Comparing UTC dates to local request dates directly would return a 404 for every such record.

### How the server handles it

`_LOCAL_TZ` is read once at startup from the `TILE_TIMEZONE` environment variable:

```python
_LOCAL_TZ = ZoneInfo(os.environ.get("TILE_TIMEZONE", "Australia/Sydney"))

def _ts_to_local_date(ts) -> str:
    return pd.Timestamp(ts).tz_localize("UTC").tz_convert(_LOCAL_TZ).strftime("%Y-%m-%d")
```

Every point where a UTC timestamp is exposed or compared is converted to local time via `_ts_to_local_date`:

- **`get_available_dates`** — converts store timestamps to local date strings. The manifest always returns values the client can round-trip back unchanged as request dates.
- **`load_slice`** — iterates all timestamps in the store's `time` coordinate, converts each to a local date via `_ts_to_local_date`, and collects those that match the requested date string exactly. The first matching timestamp is selected with `sel(time=pd.Timestamp(matching[0]))`. If multiple timestamps map to the same local date (e.g. sub-daily data), a warning is logged and the first is used. If no timestamp maps to the requested local date, `FileNotFoundError` is raised with a message indicating that dates must be in `_LOCAL_TZ` local time (not UTC). This avoids `method="nearest"` silently serving data from an adjacent day.

**Critical constraint** — `get_available_dates` and `load_slice` must always use the same `_LOCAL_TZ` value. Changing one without the other causes dates to silently mismatch: the manifest returns dates the client cannot successfully request. `TILE_TIMEZONE` is the single source of truth; never hardcode a timezone string in either function.

### Client contract

Dates in the API are **opaque keys**, not calendar dates in the client's local timezone. Clients must:

1. Fetch available dates from `/manifest`
2. Pass those exact date strings back in tile/point requests

Do not construct date strings from the client's local clock — the server interprets them as `TILE_TIMEZONE` local dates, and a client in a different timezone would produce strings that do not exist in the manifest.

### Sub-daily data

The current API is day-granularity only. If a store ever has sub-daily time resolution, multiple UTC timestamps will map to the same local date — `load_slice` logs a warning and returns the first. Supporting hourly queries would require changes to the URL structure and cache key design; this is deferred until there is a concrete use case. See the discussion in the codebase for context.

### What to check when adding a new product

If you add a product whose Zarr store timestamps are stored differently (e.g. already in local time, or in a different timezone), you must either adjust `_ts_to_local_date` or override the date handling for that product. Do not change the default without understanding this invariant first.

---

## Coordinate normalisation

On store open, `_open_store` in `services/loader.py` applies `COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}` to rename any uppercase coordinate names to lowercase. This happens once per store URL and is stored in the singleton. All downstream code (renderer, manifest, point endpoint) can always assume `lat`/`lon`/`time` regardless of what the store uses natively.

---

## Coordinate system and projection pipeline

### Server — Plate Carrée (EPSG:4326)

**Data tiles** are produced in **Plate Carrée** (equirectangular projection) — longitude maps linearly to pixel X, latitude maps linearly to pixel Y. This is the visual representation of EPSG:4326 / WGS84 geographic coordinates.

The projection is implemented implicitly in `_resample_to_grid` (`services/data_renderer.py`):

```python
target_lons = np.linspace(lon_min, lon_max, total_w)  # lon → x (linear)
target_lats = np.linspace(lat_max, lat_min, total_h)  # lat → y (linear, north→south)
```

`np.linspace` distributes points evenly in degrees — that linear mapping **is** Plate Carrée. No projection formula is needed. Tiles are essentially slices of the native lat/lon data grid with no reprojection.

The manifest returns bounds in geographic degrees (`lonMin`, `lonMax`, `latMin`, `latMax`), not projected metres.

**Visual tiles** are reprojected to Web Mercator (EPSG:3857) by rio-tiler's `XarrayReader`. The source data must be in EPSG:4326 — see [Visual renderer CRS requirements](#visual-renderer-crs-requirements).

Plate Carrée is the right choice for data tiles because:

- Source Zarr data is on a regular lat/lon grid — tiles map directly with no reprojection overhead
- Scientific accuracy is preserved — distances at different latitudes are not distorted
- Standard for oceanographic datasets (IMOS, ERA5, CMIP6 all use regular lat/lon grids)

### Frontend — Web Mercator base map

The frontend map canvas is in **Web Mercator (EPSG:3857)**. The WebGL shader converts each fragment's Mercator position to lon/lat (inverse Mercator formula), then samples the Plate Carrée tile using a linear lat/lon lookup — matching the server's `np.linspace` mapping. Data value decoding and colour ramp application happen in the same pass.

### Manifest as the contract between server and shader

The manifest is the interface between the server's coordinate system and the shader's uniforms:

| Manifest field                             | Shader uniform              | Purpose                                           |
| ------------------------------------------ | --------------------------- | ------------------------------------------------- |
| `bounds.lonMin/lonMax/latMin/latMax`       | `u_data_bounds`             | geographic extent for tile sampling               |
| `lods[n].grid`                             | `u_lod_grids`               | cols×rows per LOD for chunk lookup                |
| `valueRange`                               | `u_value_range`             | decode uint24 back to raw value (scalar products) |
| `uRange` / `vRange`                        | `u_u_range` / `u_v_range`   | decode U/V bytes back to raw values (UV products) |
| `lods[n].chunkPx` / `storedPx` / `padding` | `u_uv_scale`, `u_uv_offset` | skip padding border in atlas UV                   |

---

## PNG encoding contract

Tiles are RGBA PNGs (`optimize=False`). The byte layout is fixed and consumed by a WebGL shader:

- **24-bit scalar** (GSLA, SSTA, WDIR, etc.): R=high byte, G=mid byte, B=low byte of normalised uint24; A=ocean mask (255=ocean, 0=land). Land pixels have RGB zeroed (premultiplied form).
- **UV vector** (e.g. ocean current): R=U normalised to 8-bit, G=V normalised to 8-bit, B=ocean mask×255, A=255.

Normalisation ranges (`valueRange`, `uRange`/`vRange`) are computed from the full pre-resampled dataset and returned in `manifest.json`. All tiles for a date share the same ranges.

---

## Visual renderer CRS requirements

`services/visual_renderer.py` uses rio-tiler's `XarrayReader`, which requires data in **EPSG:4326** (geographic lat/lon degrees) with bounds strictly within `(−180, −90, 180, 90)`.

### CRS guard

`_to_scalar_parts` validates coordinate ranges before passing data to `XarrayReader`:

- `lat ∈ [−90, 90]`
- `lon ∈ [−180, 360]` (allows 0–360 convention before normalisation)

A dataset in a projected CRS (e.g. UTM, GDA94/MGA) would have coordinate values in the millions and is rejected immediately with a descriptive `ValueError`. This prevents silent mis-rendering — the hardcoded `write_crs("EPSG:4326")` call would otherwise label projected coordinates as geographic without error.

### Antimeridian handling

Some stores use longitudes that extend past 180° (e.g. GSLA: 57–185°E). `XarrayReader` rejects any bounds outside `±180`, so these must be normalised. The approach depends on the data topology:

**Detection — contiguity check**: normalise all `lon > 180` to negative values (`lon − 360`), then sort. If the maximum gap between adjacent sorted values is ≤ 2× the native resolution, the data is a contiguous global-style grid and wrap-and-sort is safe. A large gap (e.g. 232° for GSLA) means the data is a regional window straddling the antimeridian.

**Global data (contiguous after normalisation)**: standard wrap-and-sort to `[−180, 180)`.

**Regional antimeridian straddle** (e.g. GSLA 57–185°E):

The dataset is split into two segments:

| Segment | Lon range                   | Notes                           |
| ------- | --------------------------- | ------------------------------- |
| Primary | `lon < 180`                 | Native coords unchanged         |
| Minor   | `lon > 180` shifted by −360 | e.g. 180.2–185 → −179.8 to −175 |

`lon == 180` is excluded from both segments to keep each segment's half-pixel rioxarray bounds strictly inside `±180`.

Both segments are rendered independently using `XarrayReader` and the results are alpha-composited (non-transparent overlay pixels replace base pixels). Most tile/bbox requests only intersect one segment; the composite is a no-op for the non-intersecting segment. This ensures data near the antimeridian (e.g. the Tonga/Fiji strip for GSLA) is rendered correctly rather than silently dropped.

---

## Caching strategy

Three-tier cache stack ordered tiles → S3: L1 (processed grid) → L2 (in-memory slice) → L3 (disk) → S3. Visual tiles have no L1; requests hit L2 first. Cold S3 reads (~2s) are absorbed by disk (L3, ~30ms) and in-memory LRU (L2, <1ms). The disk cache is the primary mechanism for eliminating cold origin hits — it persists across server restarts and is pre-populated at startup.

**Store singleton** (`services/loader.py`, `_stores` dict keyed by URL)

Caches the open Zarr store handle (lazy, metadata only). Shared across all products using the same store.

Uses a **stale-while-revalidate** strategy to pick up newly appended time steps without ever blocking a request:

- **Startup** — `prewarm_stores` in `main.py` opens every registered store in background daemon threads so the cache is warm before the first request arrives.
- **Within TTL** — the cached store is returned immediately (sub-millisecond).
- **After TTL** (`STORE_TTL_SECONDS`, default `600`) — the stale store is returned immediately for the current request, and a single background daemon thread calls `_refresh_store_background` to re-open the store. `_store_refreshing` prevents duplicate refresh threads for the same URL.
- **First-ever open** (no cached entry) — the request blocks until `xr.open_zarr` completes; all concurrent requests for the same URL wait on the same `concurrent.futures.Future` rather than each opening independently.

Re-opening is cheap — `xr.open_zarr` reads only metadata and coordinate arrays (`time`, `lat`, `lon`), no data chunks. In-flight `load_slice` calls hold a direct Python reference to the old dataset object and complete normally. `_slice_cache` and `_processed_cache` entries for existing dates remain valid and unaffected.

**Layer 1 — Processed grid cache** (`services/data_renderer.py`, keyed `(source_path, date, variable, lod)`)

Stores the resampled + normalised numpy arrays for the full LOD grid. A hit reduces per-tile work to `_extract_chunk` + PNG encode only — no S3 I/O, no resampling. The key is semantic (not object identity), so cache hits survive L2 slice evictions and disk-reloaded slices.

Entry sizes for the satellite heatwave product (2000×3900): LOD 1 ~1.4 MB, LOD 2 ~3.3 MB, LOD 3 ~12 MB, LOD 4 ~41 MB. GSLA and radar products have only 1 LOD level at ~1.4 MB.

Size is controlled by `PROCESSED_CACHE_SIZE` (default `50`). Sized as `SLICE_CACHE_SIZE × MAX_LODS` with headroom: `10 × 4 = 40`, rounded to 50. This keeps all LOD levels warm for every date in the L2 slice cache.

On product deletion, `evict_processed_cache` is called by `evict_product_cache` in `loader.py` to purge all entries matching `source_path`.

Visual tiles do not use Layer 1 — `XarrayReader` handles its own rendering per request from the Layer 2 slice.

**Layer 2 — Slice cache, in-memory** (`services/loader.py`, `_slice_cache` keyed `(store_url, date, variables)`)

Stores a fully-computed (`.compute()`) 2D lat×lon numpy slice. Sub-millisecond on hit. Keyed by `variables` so different products using the same store cache independently.

Size is controlled by the `SLICE_CACHE_SIZE` env var (default `10`). Entry size varies significantly by product: ~2 MB for GSLA (351×641), ~61 MB for the satellite heatwave products (2000×3900 float64).

Primary consumers are **visual_tiles** (no processed grid cache above it — every tile request calls `load_slice`) and **data_tiles manifest/point** (always need `ds` directly). For data_tiles tile requests, the slice is only loaded on a `_processed_cache` miss; once the processed grid is warm, L2 is bypassed entirely.

**Layer 3 — Slice cache, disk** (`DISK_CACHE_PATH` directory, `services/loader.py`)

Persists fully-computed slices as lz4-compressed pickles to survive server restarts. On an L2 miss, `load_slice` checks disk before going to S3. A disk hit (~30ms read + decompress) is ~60× faster than a cold S3 fetch.

File layout: `{DISK_CACHE_PATH}/{store_name}-{var_str}/{date}.pkl.lz4`

Enabled by setting `DISK_CACHE_PATH` (e.g. `/app/slice_cache`). If unset, disk caching is disabled and all cold reads go directly to S3.

Environment variables:

| Variable                         | Default   | Description                                                     |
| -------------------------------- | --------- | --------------------------------------------------------------- |
| `DISK_CACHE_PATH`                | _(unset)_ | Absolute path for disk cache. Disabled if unset.                |
| `DISK_CACHE_LIMIT_GB`            | `20`      | Maximum total disk usage before eviction runs                   |
| `DISK_EVICTION_THRESHOLD`        | `0.85`    | Fraction of limit at which eviction triggers (0.0–1.0)          |
| `CACHE_DAYS`                     | `30`      | How many of each product's most-recent available dates to cache |
| `PREWARM_WORKERS`                | `4`       | Thread pool size for parallel prewarm at startup                |
| `CACHE_REFRESH_INTERVAL_SECONDS` | `14400`   | How often (seconds) the background refresh cycle runs           |

**Startup prewarm** — `prewarm_disk_slices` runs at startup (and whenever a new product is added via the admin API). It fetches the latest `CACHE_DAYS` dates for every product using a `ThreadPoolExecutor` (`PREWARM_WORKERS` workers). Each job calls `load_slice`, which reads from disk if already cached or fetches from S3 and writes to disk. For 4 products × 30 dates with 4 workers, cold prewarm takes ~60s; subsequent restarts hit disk immediately (~30ms each, ~30s total).

**Refresh cycle** — `_cache_refresh_loop` wakes every `CACHE_REFRESH_INTERVAL_SECONDS` (default 4 hours) and calls `refresh_disk_cache`. In steady state (IMOS data is daily), this adds ~1 new date per product and evicts dates that have rolled outside the `CACHE_DAYS` window. The refresh runs in a background thread via `asyncio.to_thread` — the event loop is never blocked, and concurrent requests are unaffected.

**Eviction**:

- _Stale dates_ — `refresh_disk_cache` deletes any `.pkl.lz4` files whose dates are no longer in the `CACHE_DAYS` window.
- _Disk pressure_ — `_evict_disk_if_needed` (called at the start of each refresh cycle) removes files when total usage exceeds `DISK_EVICTION_THRESHOLD × DISK_CACHE_LIMIT_GB`. Files are sorted `(size ascending, date ascending)` — small+old files are evicted first, keeping the large satellite slices that would be most expensive to re-fetch.
- _Product deletion_ — `evict_product_cache` (called by `DELETE /admin/products/{id}`) removes the product's disk directory via `shutil.rmtree` and purges matching entries from the L2 in-memory cache immediately.

Disk eviction never invalidates L2 in-memory entries — the in-memory data is still valid and serves requests until it falls out of the LRU naturally.

### Thread safety

All caches use `threading.Lock`. Concurrent requests for the same in-flight key are deduplicated via `concurrent.futures.Future`: the first thread to miss the cache creates a `Future` and does the work; all other threads arriving for the same key block on `future.result()` and receive the same result when the single computation completes. This applies to both `_slice_in_flight` (slice cache) and `_processed_inflight` (processed grid cache). Errors propagate to all waiting threads so a failed request does not permanently block future attempts for the same key.

---

## Concurrency

See [`docs/concurrency.md`](concurrency.md) for the full concurrency model, capacity evaluation, stampede protection, and scaling notes.

Cache sizing is driven by dataset slice size and concurrent active dates, not by `THREAD_POOL_SIZE`. The satellite heatwave products (2000×3900, ~61 MB/slice) dominate memory; GSLA and radar are negligible by comparison.

```
SLICE_CACHE_SIZE=10        →  PROCESSED_CACHE_SIZE=50
(concurrent active dates       (SLICE_CACHE_SIZE × MAX_LODS(4) + headroom)
 for visual_tiles + manifest)
```

`THREAD_POOL_SIZE` controls request concurrency independently. Concurrent in-flight slice loads each hold ~61 MB in RAM for the duration of the request — `_slice_in_flight` deduplicates concurrent requests for the same key, limiting peak in-flight memory to `unique_keys × slice_size` rather than `THREAD_POOL_SIZE × slice_size`.

---

## Colormap system

Visual tiles support any colormap name that resolves through the following lookup chain (first match wins):

1. **Custom registry** (`colormaps.json`) — names registered at runtime via the admin API.
2. **rio-tiler built-ins** — e.g. `viridis`, `plasma`, `inferno`.
3. **matplotlib** — any name from `matplotlib.colormaps`, including diverging maps like `RdBu_r`, `coolwarm`.

An unrecognised name returns `400 Bad Request`.

### Listing supported colormaps

`GET /visual_tiles/colormaps` returns all supported names grouped by source, with higher-priority sources excluding duplicate names from lower ones:

```json
{
  "custom":    ["imos_sst"],
  "rio_tiler": ["accent", "algae", "viridis", ...],
  "matplotlib": ["Blues", "RdBu_r", "coolwarm", ...]
}
```

### Custom colormaps

Custom colormaps are registered via `POST /admin/colormaps` and persisted in `colormaps.json`. They are loaded on startup by `load_colormaps()` in `services/colormap_store.py` and take effect immediately without a server restart. The private registry and all accessor functions live in `colormap_store.py` — no other module holds colormap state directly.

All colormaps are stored internally as 256-entry RGBA LUTs (one tuple per normalised byte value, where 0 = data minimum and 255 = data maximum after `rescale`). The `POST /admin/colormaps` payload normalises the input to this format at registration time.

### Colormap modes

The `mode` field on `POST /admin/colormaps` controls how the input stops are expanded to the 256-entry LUT:

| Mode | `entries` format | Behaviour |
|---|---|---|
| `ramp` (default) | 2–256 colour stops | Evenly-spaced stops linearly interpolated to 256 entries |
| `categorical` | dict `{"<int>": colour, ...}` | Each integer value maps to one LUT slot; all other slots are transparent |

Each colour stop (in both modes) may be a CSS hex string (`#rgb`, `#rrggbb`, `#rrggbbaa`) or a `[r, g, b, a]` list. Hex strings without alpha default to fully opaque (a=255).

**Ramp example** — 5 stops interpolated across the full LUT:

```json
{"name": "ocean_depth", "mode": "ramp", "entries": ["#000080", "#00ffff", "#ffffff", "#ff8c00", "#8b0000"]}
```

**Categorical example** — discrete class values 1–4:

```json
{
  "name": "land_cover",
  "mode": "categorical",
  "entries": {"1": "#ffff00", "2": "#0000ff", "3": "#ff0000", "4": "#000000"}
}
```

For categorical colormaps, `rescale=min,max` is **required** at render time and must match the range of the integer keys (e.g. `?rescale=1,4`). Omitting `rescale` with a categorical colormap returns `400 Bad Request`. This is enforced because the renderer auto-rescales to the per-tile data range when `rescale` is absent, which would corrupt the LUT slot mapping.

The data range for a categorical colormap is inferred from the key range (`min(keys)` → `max(keys)`) at registration time and used to place each value in the LUT. Values not covered by any key render as fully transparent.

### Cache behaviour

`_colormap()` in `services/visual_renderer.py` is `@lru_cache`-d (max 64 entries). The cache is cleared automatically whenever a colormap is added or deleted via the admin API — `colormap_store._reload()` calls `_colormap.cache_clear()` after every write.

---

## Adding a new product

Products are managed at runtime via the admin API — no code changes or redeploy required. All products are persisted in `products.json` (the single source of truth) and loaded into memory on startup.

### Via admin API

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

# Remove a product
curl -X DELETE http://localhost:8000/admin/products/my_product \
  -H "X-Admin-Key: your-secret-key"
```

On the first request after registration:

- The store is opened and coordinates are normalised automatically
- LOD grids are computed from the store's actual lat/lon dimensions
- Rendering and manifest generation work generically from `product.variable`
- Disk cache prewarm is triggered immediately in a background thread

### Requirements for the Zarr store

| Requirement        | Detail                                                                                                                                                                                                  |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Coordinate names   | Must be `lat`/`lon`/`time`, or the uppercase variants `LATITUDE`/`LONGITUDE`/`TIME` (renamed automatically on open). If a store uses different names, add a mapping to `COORD_NAMES` in `constants.py`. |
| Spatial dimensions | `lat` and `lon` must be present after normalisation — `_open_store` raises `ValueError` with a clear message if not.                                                                                    |
| Variable           | The variable(s) named in `Product.variable` must exist in the store.                                                                                                                                    |

### Optional overrides

`Product` fields can be customised per product if the defaults don't fit:

| Field       | Default              | When to override                                          |
| ----------- | -------------------- | --------------------------------------------------------- |
| `chunk_px`  | `(240, 192)`         | Store has very small or very large spatial extent         |
| `padding`   | `1`                  | Tile edge artefacts, or no padding needed                 |
| `lod_grids` | `{}` (auto-computed) | Pre-set known grids to skip the first-request computation |
