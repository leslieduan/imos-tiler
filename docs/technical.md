# Technical Reference

## Background: Why Zarr

The NetCDF/HDF5 stack had an unacceptable cold-start cost for cloud-native serving. HDF5 B-tree traversal requires hundreds of sequential HTTP round-trips regardless of what code does — it is a file format constraint, not fixable in the application layer. Observed cold starts from home internet: ssta ~30s, Marine Heatwave 90s+ (8m 34s TTFB measured). Even in-region on AWS, Marine Heatwave takes 2–4s on cold start due to its 15 variables × 7.8M pixel grid.

Zarr eliminates this entirely: metadata is one `.zmetadata` HTTP request, and variable chunks are directly addressable with no traversal. The NetCDF stack has been removed.

**Full format analysis and IMOS product file details: `docs/netcdf-vs-zarr.md`.**

---

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │           Frontend (WebGL/Map)        │
                    └──────────────────┬───────────────────┘
                                       │ HTTP
                    ┌──────────────────▼───────────────────┐
                    │            FastAPI  main.py           │
                    └──────┬───────────────────┬───────────┘
                           │                   │
             ┌─────────────▼──────┐  ┌─────────▼──────────┐
             │  routers/          │  │  routers/           │
             │  data_tiles.py     │  │  visual_tiles.py    │
             │  /data_tiles       │  │  /visual_tiles      │
             └──────┬─────────────┘  └──────┬──────────────┘
                    │  shared                │  shared
                    └───────┬───────────────┘
                    ┌───────▼──────────────┐
                    │  routers/products.py  │
                    │  /products /manifest  │
                    │  /{id}/{date}/point   │
                    └───────────────────────┘
                           │
          ┌────────────────┴─────────────────┐
          │                                   │
┌─────────▼──────────┐             ┌──────────▼──────────┐
│  services/         │             │  services/           │
│  loader.py         │             │  data_renderer.py    │
│                    │             │  visual_renderer.py  │
│  store singleton   │             │                      │
│  L1 slice cache    │             │  processed grid      │
│  (url,date,vars)   │             │  cache LRU (data)    │
└─────────┬──────────┘             │  XarrayReader (vis.) │
          │ L1 miss                └─────────────────────-┘
┌─────────▼──────────┐
│  Disk cache        │
│  slice_cache/      │
│  lz4 pkl per date  │
└─────────┬──────────┘
          │ L2 miss
┌─────────▼────────┐
│     AWS S3       │
│   Zarr stores    │
└──────────────────┘
```

### Request flow

**Data tiles** (`/data_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png`):

```
S3 cold   → get_lod_grids (opens store, computes lod_grids) → load_slice (S3 .compute(), ~2s)  → _get_processed (resample) → _extract_chunk → PNG encode
disk warm → get_lod_grids (already set)                     → load_slice (disk read, ~30ms)     → _get_processed (resample) → _extract_chunk → PNG encode
mem warm  → get_lod_grids (already set)                     → load_slice (L1 hit, <1ms)         → _get_processed (cache hit) → _extract_chunk → PNG encode
```

**Visual tiles** (`/visual_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png` or `/bbox`):

```
S3 cold   → load_slice (S3 .compute(), ~2s)  → _to_scalar_parts (antimeridian split if needed) → XarrayReader.tile/part → colormap + PNG encode
disk warm → load_slice (disk read, ~30ms)     → _to_scalar_parts → XarrayReader.tile/part → colormap + PNG encode
mem warm  → load_slice (L1 hit, <1ms)         → _to_scalar_parts → XarrayReader.tile/part → colormap + PNG encode
```

Visual tiles use `rio-tiler`'s `XarrayReader` for Web Mercator reprojection; no processed grid cache is involved.

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
    visual_tiles.py              ← /visual_tiles — colourised Web Mercator XYZ tiles + bbox
    products.py                  ← shared: /products, /manifest, /{id}/{date}/point — included by both tile routers
    admin.py                     ← /admin — product and colormap management (key-protected)
  slice_cache/                   ← disk slice cache (lz4-compressed pickles, runtime, gitignored)
  services/
    loader.py                    ← Zarr store singleton + L1 slice cache + disk cache + get_lod_grids
    data_renderer.py             ← processed grid cache + chunk extract + PNG encode (data tiles)
    visual_renderer.py           ← Web Mercator tile render + bbox render + colormap lookup (visual tiles)
    product_store.py             ← products.json read/write + in-memory PRODUCTS dict management
    colormap_store.py            ← colormaps.json read/write + in-memory CUSTOM_COLORMAPS management
```

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

| Query param | Default | Description |
|---|---|---|
| `colormap` | `viridis` | Colormap name — rio-tiler built-in, matplotlib name, or custom registered name |
| `rescale` | data min/max for the date | Value range as `min,max`, e.g. `-0.5,0.5` |

Bbox-specific query parameters:

| Query param | Default | Description |
|---|---|---|
| `bbox` | required | Bounding box as `minx,miny,maxx,maxy` in the CRS specified by `crs` |
| `width` | `256` | Output image width in pixels (1–2048) |
| `height` | `256` | Output image height in pixels (1–2048) |
| `crs` | `EPSG:4326` | CRS of the bbox coordinates. `EPSG:4326` for geographic degrees; `EPSG:3857` for Web Mercator meters (Mapbox `{bbox-epsg-3857}`) |

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

| Product               | Variable(s) | Zarr store                                       |
| --------------------- | ----------- | ------------------------------------------------ |
| Sea level anomaly     | GSLA        | `model_sea_level_anomaly_gridded_realtime.zarr`  |
| Ocean current         | UCUR, VCUR  | `model_sea_level_anomaly_gridded_realtime.zarr`  |
| Radar wind (SA Gulfs) | WDIR        | `radar_SouthAustraliaGulfs_wind_delayed_qc.zarr` |
| AusTemp heatwave SSTA | ssta        | `satellite_austemp_heatwave_8day.zarr`           |

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
4. Calls `product.apply_computed_lod_grids(data_width, data_height)`, which runs `_compute_lod_grids` and writes the result back via `object.__setattr__` (bypasses `frozen=True` for this one field only)
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

| Manifest field | Shader uniform | Purpose |
|---|---|---|
| `bounds.lonMin/lonMax/latMin/latMax` | `u_data_bounds` | geographic extent for tile sampling |
| `lods[n].grid` | `u_lod_grids` | cols×rows per LOD for chunk lookup |
| `valueRange` | `u_value_range` | decode uint24 back to raw value (scalar products) |
| `uRange` / `vRange` | `u_u_range` / `u_v_range` | decode U/V bytes back to raw values (UV products) |
| `lods[n].chunkPx` / `storedPx` / `padding` | `u_uv_scale`, `u_uv_offset` | skip padding border in atlas UV |

---

## PNG encoding contract

Tiles are RGBA PNGs (`optimize=False`). The byte layout is fixed and consumed by a WebGL shader:

- **24-bit scalar** (GSLA, SSTA, WDIR, etc.): R=high byte, G=mid byte, B=low byte of normalised uint24; A=ocean mask (255=ocean, 0=land, premultiplied).
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

| Segment | Lon range | Notes |
|---|---|---|
| Primary | `lon < 180` | Native coords unchanged |
| Minor | `lon > 180` shifted by −360 | e.g. 180.2–185 → −179.8 to −175 |

`lon == 180` is excluded from both segments to keep each segment's half-pixel rioxarray bounds strictly inside `±180`.

Both segments are rendered independently using `XarrayReader` and the results are alpha-composited (non-transparent overlay pixels replace base pixels). Most tile/bbox requests only intersect one segment; the composite is a no-op for the non-intersecting segment. This ensures data near the antimeridian (e.g. the Tonga/Fiji strip for GSLA) is rendered correctly rather than silently dropped.

---

## Caching strategy

Three-tier cache stack. Cold S3 reads (~2s) are absorbed by disk (L2, ~30ms) and in-memory LRU (L1, <1ms). The disk cache is the primary mechanism for eliminating cold origin hits — it persists across server restarts and is pre-populated at startup.

**Layer 1 — Store singleton** (`services/loader.py`, `_stores` dict keyed by URL)

Caches the open Zarr store handle (lazy, metadata only). Shared across all products using the same store.

Uses a **stale-while-revalidate** strategy to pick up newly appended time steps without ever blocking a request:

- **Startup** — `prewarm_stores` in `main.py` opens every registered store in background daemon threads so the cache is warm before the first request arrives.
- **Within TTL** — the cached store is returned immediately (sub-millisecond).
- **After TTL** (`STORE_TTL_SECONDS`, default `600`) — the stale store is returned immediately for the current request, and a single background daemon thread calls `_refresh_store_background` to re-open the store. `_store_refreshing` prevents duplicate refresh threads for the same URL.
- **First-ever open** (no cached entry) — the request blocks until `xr.open_zarr` completes; all concurrent requests for the same URL wait on the same `Future` rather than each opening independently.

Re-opening is cheap — `xr.open_zarr` reads only metadata and coordinate arrays (`time`, `lat`, `lon`), no data chunks. In-flight `load_slice` calls hold a direct Python reference to the old dataset object and complete normally. `_slice_cache` and `_processed_cache` entries for existing dates remain valid and unaffected.

**Layer 2 — Slice cache, in-memory** (`services/loader.py`, `_slice_cache` keyed `(store_url, date, variables)`)

Stores a fully-computed (`.compute()`) 2D lat×lon numpy slice. Sub-millisecond on hit. Keyed by `variables` so different products using the same store cache independently.

Size is controlled by the `SLICE_CACHE_SIZE` env var (default `100`). Each entry is roughly 2–7 MB depending on grid size and number of variables. Should be kept at least equal to `THREAD_POOL_SIZE` so a burst of cold requests does not immediately evict freshly computed slices.

`id(ds)` is stable for as long as a slice is held in `_slice_cache` — this is the anchor for Layer 4.

**Layer 3 — Slice cache, disk** (`slice_cache/` directory, `services/loader.py`)

Persists fully-computed slices as lz4-compressed pickles to survive server restarts. On an L2 miss, `load_slice` checks disk before going to S3. A disk hit (~30ms read + decompress) is ~60× faster than a cold S3 fetch.

File layout: `{DISK_CACHE_PATH}/{store_name}-{var_str}/{date}.pkl.lz4`

Enabled by setting `DISK_CACHE_PATH` (e.g. `/app/slice_cache`). If unset, disk caching is disabled and all cold reads go directly to S3.

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `DISK_CACHE_PATH` | _(unset)_ | Absolute path for disk cache. Disabled if unset. |
| `DISK_CACHE_LIMIT_GB` | `20` | Maximum total disk usage before eviction runs |
| `DISK_EVICTION_THRESHOLD` | `0.85` | Fraction of limit at which eviction triggers (0.0–1.0) |
| `CACHE_DAYS` | `30` | How many of each product's most-recent available dates to cache |
| `PREWARM_WORKERS` | `4` | Thread pool size for parallel prewarm at startup |
| `CACHE_REFRESH_INTERVAL_SECONDS` | `14400` | How often (seconds) the background refresh cycle runs |

**Startup prewarm** — `prewarm_disk_slices` runs at startup (and whenever a new product is added via the admin API). It fetches the latest `CACHE_DAYS` dates for every product using a `ThreadPoolExecutor` (`PREWARM_WORKERS` workers). Each job calls `load_slice`, which reads from disk if already cached or fetches from S3 and writes to disk. For 4 products × 30 dates with 4 workers, cold prewarm takes ~60s; subsequent restarts hit disk immediately (~30ms each, ~30s total).

**Refresh cycle** — `_cache_refresh_loop` wakes every `CACHE_REFRESH_INTERVAL_SECONDS` (default 4 hours) and calls `refresh_disk_cache`. In steady state (IMOS data is daily), this adds ~1 new date per product and evicts dates that have rolled outside the `CACHE_DAYS` window. The refresh runs in a background thread via `asyncio.to_thread` — the event loop is never blocked, and concurrent requests are unaffected.

**Eviction**:
- *Stale dates* — `refresh_disk_cache` deletes any `.pkl.lz4` files whose dates are no longer in the `CACHE_DAYS` window.
- *Disk pressure* — `_evict_disk_if_needed` (called at the start of each refresh cycle) removes files when total usage exceeds `DISK_EVICTION_THRESHOLD × DISK_CACHE_LIMIT_GB`. Files are sorted `(size ascending, date ascending)` — small+old files are evicted first, keeping the large satellite slices that would be most expensive to re-fetch.
- *Product deletion* — `evict_product_cache` (called by `DELETE /admin/products/{id}`) removes the product's disk directory via `shutil.rmtree` and purges matching entries from the in-memory L2 cache immediately.

Disk eviction never invalidates L2 in-memory entries — the in-memory data is still valid and serves requests until it falls out of the LRU naturally.

**Layer 4 — Processed grid cache** (`services/data_renderer.py`, keyed `(id(ds), lod)`)

Stores the resampled + normalised numpy arrays for the full LOD grid. A hit reduces per-tile work to `_extract_chunk` + PNG encode only — no S3 I/O, no resampling. `id(ds)` is stable because `ds` is held alive by Layer 2.

Size is controlled by the `PROCESSED_CACHE_SIZE` env var (default `400`). Should be set to at least `SLICE_CACHE_SIZE × number_of_LOD_levels` so every cached slice can have its processed grids cached too.

Visual tiles do not use Layer 4 — `XarrayReader` handles its own rendering per request from the Layer 2 slice.

### Thread safety

All caches use `threading.Lock`. The processed grid cache additionally uses a `threading.Event` per in-flight key: concurrent requests for the same `(ds, lod)` wait for the first computation to complete rather than duplicating it.

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

## Colormap system

Visual tiles support any colormap name that resolves through the following lookup chain (first match wins):

1. **Custom registry** (`CUSTOM_COLORMAPS` in `constants.py` / `colormaps.json`) — names registered at runtime via the admin API.
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

Each custom colormap is a list of exactly 256 RGBA tuples — one per normalised byte value (0 = data minimum, 255 = data maximum after rescaling).

`CUSTOM_COLORMAPS` in `constants.py` is empty `{}` by default. Runtime additions are persisted in `colormaps.json` and managed via the admin API (`POST /admin/colormaps`, `DELETE /admin/colormaps/{name}`). They are loaded on startup by `load_colormaps()` in `services/colormap_store.py` and take effect immediately without a server restart.

### Cache behaviour

`_colormap()` in `services/visual_renderer.py` is `@lru_cache`-d (max 64 entries). The cache is cleared automatically whenever a colormap is added, updated, or deleted via the admin API.

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

### Requirements for the Zarr store

| Requirement | Detail |
|---|---|
| Coordinate names | Must be `lat`/`lon`/`time`, or the uppercase variants `LATITUDE`/`LONGITUDE`/`TIME` (renamed automatically on open). If a store uses different names, add a mapping to `COORD_NAMES` in `constants.py`. |
| Spatial dimensions | `lat` and `lon` must be present after normalisation — `_open_store` raises `ValueError` with a clear message if not. |
| Variable | The variable(s) named in `Product.variable` must exist in the store. |

### Optional overrides

`Product` fields can be customised per product if the defaults don't fit:

| Field | Default | When to override |
|---|---|---|
| `chunk_px` | `(240, 192)` | Store has very small or very large spatial extent |
| `padding` | `1` | Tile edge artefacts, or no padding needed |
| `lod_grids` | `{}` (auto-computed) | Pre-set known grids to skip the first-request computation |
