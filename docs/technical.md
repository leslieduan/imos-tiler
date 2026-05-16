# Technical Reference

---

## Table of contents

1. [Overview](#1-overview)
2. [Why Zarr](#2-why-zarr)
3. [System architecture](#3-system-architecture)
4. [File layout](#4-file-layout)
5. [Tile coordinate systems and projection pipeline](#5-tile-coordinate-systems-and-projection-pipeline)
6. [URL contract and API surface](#6-url-contract-and-api-surface)
7. [LOD grid system](#7-lod-grid-system)
8. [PNG encoding contract (data tiles)](#8-png-encoding-contract-data-tiles)
9. [Visual renderer specifics (CRS guard and antimeridian)](#9-visual-renderer-specifics-crs-guard-and-antimeridian)
10. [Date and timezone convention](#10-date-and-timezone-convention)
11. [Coordinate normalisation](#11-coordinate-normalisation)
12. [Caching strategy](#12-caching-strategy)
13. [Background tasks](#13-background-tasks)
14. [Concurrency: event loop and threading](#14-concurrency-event-loop-and-threading)
15. [Capacity and resource planning](#15-capacity-and-resource-planning)
16. [Colormap system](#16-colormap-system)
17. [Adding a new product](#17-adding-a-new-product)
18. [Environment variables](#18-environment-variables)

---

## 1. Overview

The server is a FastAPI application that produces on-demand PNG tiles for IMOS ocean data products held in Zarr stores on S3. It exposes **two independent tile pipelines** from the same underlying data:

| Pipeline        | Output CRS               | Coordinate convention                                        | Consumer                                             |
| --------------- | ------------------------ | ------------------------------------------------------------ | ---------------------------------------------------- |
| `/data_tiles`   | EPSG:4326 (Plate Carrée) | Custom LOD pyramid: `z` = LOD level, `x`/`y` = chunk col/row | WebGL shader (decodes raw values, reprojects on GPU) |
| `/visual_tiles` | EPSG:3857 (Web Mercator) | Standard XYZ slippy-map tiles (OSM/MapboxGL/Leaflet)         | Any map library / WMS-style consumer                 |

The same Zarr slice is the source for both pipelines; they diverge at the renderer. See [§5](#5-tile-coordinate-systems-and-projection-pipeline) and [`docs/tile_system.md`](tile_system.md) for the full distinction.

Products are **runtime-managed** through the admin API — there is no static product list compiled into the code. Adding or removing a product takes effect immediately without restart (see [§17](#17-adding-a-new-product)).

---

## 2. Why Zarr

The NetCDF/HDF5 stack had an unacceptable cold-start cost for cloud-native serving. HDF5 B-tree traversal requires hundreds of sequential HTTP round-trips regardless of what the application does — it is a file-format constraint, not fixable in the application layer. Observed cold starts from home internet: GSLA SSTA ~30s, Marine Heatwave 90s+ (8m 34s TTFB measured). Even in-region on AWS, Marine Heatwave takes 2–4s on cold start due to its 15 variables × 7.8M-pixel grid.

Zarr eliminates this: metadata is one `.zmetadata` HTTP request, and variable chunks are directly addressable with no traversal. The NetCDF stack has been removed.

**Full format analysis and IMOS product file details: [`docs/netcdf-vs-zarr.md`](netcdf-vs-zarr.md).**

---

## 3. System architecture

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
│             event loop  +  anyio thread pool (THREAD_POOL_SIZE)            │
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
│   EPSG:4326 (Plate Carrée)         │  │   EPSG:4326 → EPSG:3857            │
│   L1 Processed grid cache          │  │   XarrayReader  (rio-tiler)        │
│   PNG encode for WebGL shader      │  │   Colormap LUT + PNG encode        │
└──────────────────┬─────────────────┘  └──────────────────┬─────────────────┘
                   │ L1 miss                               │ every request
                   └──────────────────┬────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                              loader.py                                     │
│   Store singleton                   L2 Slice cache (in-memory LRU)         │
│   (stale-while-revalidate)          keyed (url, date, vars)                │
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

### Request flow

**Data tiles** (`/data_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png`)

`load_slice` is lazy — the handler passes a callable to `render_tile`, which only invokes it when `_get_processed` misses. On a processed-cache hit, no slice I/O occurs.

```
processed warm → get_lod_grids (already set) → _get_processed (cache hit)                            → _extract_chunk → PNG encode
slice warm     → get_lod_grids (already set) → _get_processed miss → load_slice (L2 hit, <1ms)       → resample → cache → _extract_chunk → PNG encode
disk warm      → get_lod_grids (already set) → _get_processed miss → load_slice (disk read, ~30ms)   → resample → cache → _extract_chunk → PNG encode
S3 cold        → get_lod_grids (already set) → _get_processed miss → load_slice (S3 .compute(), ~2s) → resample → cache → _extract_chunk → PNG encode
```

**Visual tiles** (`/visual_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png` or `/bbox`)

No L1 cache. Every request calls `load_slice`, then `XarrayReader` reprojects to Web Mercator.

```
mem warm  → load_slice (L2 hit, <1ms)        → _to_scalar_parts (antimeridian split if needed) → XarrayReader.tile/part → colormap + PNG encode
disk warm → load_slice (disk read, ~30ms)    → _to_scalar_parts → XarrayReader.tile/part → colormap + PNG encode
S3 cold   → load_slice (S3 .compute(), ~2s)  → _to_scalar_parts → XarrayReader.tile/part → colormap + PNG encode
```

---

## 4. File layout

```
titiler-project/
  main.py                        ← mounts all routers, CORS middleware, lifespan startup
  constants.py                   ← Product dataclass + LOD algorithm + LODConfig (server-shader contract)
  products.json                  ← persisted product registrations (runtime, gitignored; local-dev default)
  colormaps.json                 ← persisted custom colormap registrations (runtime, gitignored; local-dev default)
  data/
    products.json                ← Docker: mounted volume, set via PRODUCTS_CONFIG_PATH=data/products.json
    colormaps.json               ← Docker: mounted volume, set via COLORMAPS_CONFIG_PATH=data/colormaps.json
  docs/
    technical.md                 ← this file
    tile_system.md               ← focused explanation of the two tile coordinate systems
    concurrency.md               ← concurrency model, capacity tables, stampede protection
    cache_analysis.md            ← cache design decision record (Redis vs EFS vs EBS vs ephemeral)
    dataset.md                   ← per-store variable / dimension / chunking reference
    netcdf-vs-zarr.md            ← format comparison, IMOS product file analysis, performance data
    security.md                  ← admin endpoint protection (key + nginx + EC2 security group)
  routers/
    data_tiles.py                ← /data_tiles — raw value-encoded RGBA tiles for WebGL
    visual_tiles.py              ← /visual_tiles — colourised Web Mercator XYZ tiles + bbox
    products.py                  ← shared: /products, /manifest, /{id}/{date}/point — included by both tile routers
    admin.py                     ← /admin — product and colormap management (key-protected)
  services/
    loader.py                    ← Zarr store singleton + L2 slice cache + L3 disk cache + LOD grid lazy init
    data_renderer.py             ← processed grid cache + chunk extract + PNG encode (data tiles)
    visual_renderer.py           ← Web Mercator render + bbox render + colormap lookup + legend (visual tiles)
    product_store.py             ← products.json read/write + in-memory PRODUCTS dict management
    colormap_store.py            ← colormaps.json read/write + in-memory colormap registry + ColormapMode type
  utils/
    dates.py                     ← LOCAL_TZ + ts_to_local_date + three_months_ago
    geo.py                       ← dataset_bounds + json_safe_float
    colors.py                    ← hex parsing + ramp/categorical LUT builders
```

`products.json` and `colormaps.json` default to the project root in local dev. In Docker (`docker-compose.yml`), they are overridden to `data/products.json` and `data/colormaps.json`, backed by a `./data` host volume. The L3 disk-cache directory is set via `DISK_CACHE_PATH` (default: unset in local dev; `/app/slice_cache` in Docker, backed by a `./slice_cache` host volume).

---

## 5. Tile coordinate systems and projection pipeline

The server produces tiles in **two different coordinate reference systems** depending on the endpoint. Understanding this is critical — the two pipelines look superficially similar but cannot be mixed.

### 5.1 Two pipelines, two CRSs

|                            | `/data_tiles`                                                                       | `/visual_tiles`                                                                  |
| -------------------------- | ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| **Output CRS**             | EPSG:4326 (Plate Carrée)                                                            | EPSG:3857 (Web Mercator)                                                         |
| **Tile coordinate scheme** | Custom — `z`=LOD index, `x`/`y`=chunk col/row inside the product's data extent      | Standard XYZ — `z`=zoom level, `x`/`y` over the full Mercator world (`0..2^z−1`) |
| **Pixel content**          | Raw value packed into RGBA bytes (24-bit normalised uint or two 8-bit U/V channels) | Colourised RGBA image after applying a colormap LUT                              |
| **Reprojection happens…**  | In the **WebGL fragment shader** on the client, on the GPU                          | On the **server**, by `rio-tiler`'s `XarrayReader.tile(...)`                     |
| **Out-of-bounds tile**     | HTTP 404                                                                            | Transparent 256×256 PNG (spatial), HTTP 400 (invalid coords)                     |
| **Multi-variable support** | Yes (UV products such as `ocean_current`)                                           | No (single-variable products only)                                               |
| **Manifest endpoint**      | Yes (`/manifest.json` per product/date)                                             | Not applicable                                                                   |

The data-tiles `z` axis indexes a **custom LOD pyramid** anchored to the product's own extent — see [§7](#7-lod-grid-system) for the algorithm that derives the pyramid from each Zarr store's dimensions, and [`docs/tile_system.md`](tile_system.md) for a focused walk-through of `z`/`x`/`y` semantics in both pipelines.

### 5.2 Data tiles — generated in EPSG:4326 (Plate Carrée)

Source Zarr data lives on a regular lat/lon grid. Data tiles preserve that grid exactly: longitude maps linearly to pixel X, latitude maps linearly to pixel Y. This is Plate Carrée — the visual representation of EPSG:4326 / WGS84 geographic coordinates.

The projection is implemented implicitly in `_resample_to_grid` (`services/data_renderer.py`):

```python
target_lons = np.linspace(lon_min, lon_max, total_w)  # lon → x (linear in degrees)
target_lats = np.linspace(lat_max, lat_min, total_h)  # lat → y (linear, north→south)
result = ds.interp(lon=target_lons, lat=target_lats, method="linear")
```

`np.linspace` distributes points evenly in degrees — that linear mapping **is** Plate Carrée. No projection formula is needed. Tiles are slices of the native lat/lon data grid with no reprojection on the server.

The manifest returns geographic bounds (`lonMin`, `lonMax`, `latMin`, `latMax`) in degrees, not projected metres.

**Why EPSG:4326 for data tiles**

- Source Zarr data is already on a regular lat/lon grid — tiles map directly with no reprojection overhead.
- Raw scientific values are preserved exactly; resampling is the only transform applied.
- Standard for oceanographic datasets (IMOS, ERA5, CMIP6 all use regular lat/lon grids).
- A reprojection on the server side would either lossy-resample again or require per-tile inverse-Mercator math — both wasteful when the WebGL shader can do the equivalent operation on the GPU at zero marginal cost per fragment.

### 5.3 Visual tiles — generated in EPSG:3857 (Web Mercator)

`services/visual_renderer.py` calls `XarrayReader.tile(x, y, z, reproject_method="bilinear")`. The reader internally:

1. Reads the source slice (already tagged `EPSG:4326` via `da.rio.write_crs("EPSG:4326")`).
2. Computes the Web Mercator footprint of the target tile from `(x, y, z)`.
3. Reprojects the relevant 4326 region into a 256×256 Mercator-grid array using bilinear interpolation.
4. Returns the array, which the renderer then rescales (per `rescale` or auto-derived min/max), maps through the colormap LUT, and PNG-encodes.

Because the output PNG is already in Web Mercator, visual tiles work directly with any map library that consumes XYZ Web Mercator tiles — MapboxGL `raster` sources, Leaflet, OpenLayers, Mapbox `{bbox-epsg-3857}` raster placeholders, etc. **No client-side reprojection is required.**

The `/bbox` endpoint follows the same pipeline using `reader.part(...)`; it accepts the bbox in either EPSG:4326 or EPSG:3857 (controlled by `?crs=`) and produces a Web Mercator PNG.

### 5.4 Frontend integration

The frontend in production renders a Web Mercator base map (typical of every map library: Mapbox, MapLibre, Leaflet, OpenLayers, Google Maps).

- **Visual tiles** plug straight into a `raster` source — no shader and no per-frame math.
- **Data tiles** are sampled by a custom **WebGL fragment shader**. The shader does the work the server intentionally skipped: for each fragment's Mercator position it computes the inverse Mercator to recover `(lon, lat)`, then samples the Plate-Carrée atlas via a linear lat/lon lookup — matching the server's `np.linspace` mapping. Value decoding (uint24 → float via the manifest's `valueRange`) and colour-ramp lookup happen in the same pass.

### 5.5 The manifest is the contract between server and shader

The manifest (data-tile pipeline only) is the interface between the server's coordinate system and the WebGL shader's uniforms:

| Manifest field                             | Shader uniform              | Purpose                                           |
| ------------------------------------------ | --------------------------- | ------------------------------------------------- |
| `bounds.lonMin/lonMax/latMin/latMax`       | `u_data_bounds`             | geographic extent for tile sampling               |
| `lods[n].grid`                             | `u_lod_grids`               | cols × rows per LOD for chunk lookup              |
| `valueRange`                               | `u_value_range`             | decode uint24 back to raw value (scalar products) |
| `uRange` / `vRange`                        | `u_u_range` / `u_v_range`   | decode U/V bytes back to raw values (UV products) |
| `lods[n].chunkPx` / `storedPx` / `padding` | `u_uv_scale`, `u_uv_offset` | skip padding border in atlas UV                   |

---

## 6. URL contract and API surface

`z`/`x`/`y` mean different things in each tile API — see §5 and [`docs/tile_system.md`](tile_system.md).

### 6.1 Shared endpoints (mounted under both `/data_tiles` and `/visual_tiles`)

`routers/products.py` is included by both tile routers, so these paths exist under both prefixes:

```
GET /{prefix}/products                                    → list all registered products
GET /{prefix}/manifest?from=YYYY-MM-DD&to=YYYY-MM-DD     → available dates for all products
GET /{prefix}/{product_id}/{date}/point?lat=&lon=         → variable value at point
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

**Performance**: dates are read from the `time` coordinate of each Zarr store — a 1-D array held in the store singleton. No spatial data chunks are touched. Filtering is an in-memory string comparison. Responses are sub-millisecond once the store is warm.

### 6.2 Data tiles (`/data_tiles`)

```
GET /data_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png → raw RGBA PNG tile
GET /data_tiles/{product_id}/{date}/manifest.json         → bounds + value ranges + LOD grid config
```

`z` = LOD level, `x` = chunk column (`0` = westernmost), `y` = chunk row (`0` = northernmost).

### 6.3 Visual tiles (`/visual_tiles`)

Colourised PNG tiles in standard Web Mercator (XYZ). Single-variable products only.

```
GET /visual_tiles/colormaps                                            → all supported colormap names
GET /visual_tiles/colormaps/{name}/legend                              → color legend PNG for a colormap
GET /visual_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png            → colourised Web Mercator PNG
GET /visual_tiles/{product_id}/{date}/bbox?bbox=minx,miny,maxx,maxy   → colourised PNG for arbitrary bbox
```

**Legend query parameters:**

| Query param   | Default      | Description                                                                        |
| ------------- | ------------ | ---------------------------------------------------------------------------------- |
| `rescale`     | _(none)_     | Value range as `min,max`. When provided, tick labels at lo, mid, and hi are drawn. |
| `width`       | `256`        | Image width in pixels (10–2048)                                                    |
| `height`      | `40`         | Image height in pixels (10–2048)                                                   |
| `orientation` | `horizontal` | `horizontal` (bar runs left→right) or `vertical` (bar runs top→bottom, hi at top)  |

Without `rescale`, only the color bar is rendered. With `rescale`, 20 pixels alongside the bar are reserved for labels (reducing the bar to `height-20` or `width-20` depending on orientation). Categorical colormaps render discrete equal-width color blocks (one per registered category) rather than a smooth gradient.

`z`/`x`/`y` are standard Web Mercator tile coordinates (OpenStreetMap, MapboxGL, Leaflet, etc.).

**Visual tile query parameters:**

| Query param | Default                   | Description                                                                    |
| ----------- | ------------------------- | ------------------------------------------------------------------------------ |
| `colormap`  | `viridis`                 | Colormap name — rio-tiler built-in, matplotlib name, or custom registered name |
| `rescale`   | data min/max for the date | Value range as `min,max`, e.g. `-0.5,0.5`                                      |

**Bbox-specific query parameters:**

| Query param | Default     | Description                                                                                                                      |
| ----------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `bbox`      | required    | Bounding box as `minx,miny,maxx,maxy` in the CRS specified by `crs`                                                              |
| `width`     | `256`       | Output image width in pixels (1–2048)                                                                                            |
| `height`    | `256`       | Output image height in pixels (1–2048)                                                                                           |
| `crs`       | `EPSG:4326` | CRS of the bbox coordinates. `EPSG:4326` for geographic degrees; `EPSG:3857` for Web Mercator metres (Mapbox `{bbox-epsg-3857}`) |

### 6.4 Admin API (`/admin`)

All endpoints require the `X-Admin-Key` header. The expected value is read from the `ADMIN_API_KEY` environment variable. See [`docs/security.md`](security.md) for the full key/nginx/security-group layering.

```
POST   /admin/products              → register a new product
DELETE /admin/products/{product_id} → remove a product

POST   /admin/colormaps             → register a custom colormap
DELETE /admin/colormaps/{name}      → remove a custom colormap
```

---

## 7. LOD grid system

The LOD pyramid applies to **data tiles only** (visual tiles use Web Mercator zoom levels — see [§5](#5-tile-coordinate-systems-and-projection-pipeline) for the CRS context). This section covers how `product.lod_grids` is derived from the underlying Zarr store's dimensions.

### 7.1 Constants (`constants.py`)

The three LOD knobs are bundled into a single frozen-dataclass instance, `LOD = LODConfig()`. They are **not** environment variables — these values are baked into the WebGL shader on the frontend, so changing one without redeploying the frontend silently corrupts the rendering.

- `LOD.max_lods = 4` — cap on LOD levels per product. The frontend packs all LODs into a single WebGL texture atlas hard-capped at 4096×4096 px (≈64 MB VRAM per atlas) regardless of `gl.MAX_TEXTURE_SIZE`. Going above 4 doesn't break rendering — the atlas falls back to LRU eviction — but causes visible tile re-upload churn as the user pans or zooms. `4` is tuned to fit comfortably under the cap for current product sizes.
- `LOD.min_coarsest = (2, 2)` — minimum (cols, rows) for the coarsest LOD level; levels below this are dropped. If all levels are filtered out (data smaller than one chunk), falls back to the native finest grid so there is always at least one LOD.
- `LOD.zoom_thresholds: dict[int, int]` — universal map-zoom thresholds applied to all products (e.g. `{2: 4, 3: 5, 4: 6}`).

### 7.2 Algorithm (`Product._compute_lod_grids` in `constants.py`)

Derives LOD grids from actual data dimensions and chunk size. Accepts `max_lods` and `min_coarsest` as parameters (defaulting to the constants above).

1. Finest level: `ceil(data_width / chunk_w) × ceil(data_height / chunk_h)`.
2. Depth: `floor(log2(max(finest_cols, finest_rows)))` — number of halvings before both axes reach 1 (uses `max` so elongated grids go as deep as the wider axis allows).
3. Each level `k`: `(ceil(finest_cols / 2^k), ceil(finest_rows / 2^k))` — `ceil` preserves coverage at intermediate scales (e.g. `finest=5` → `3, 2` not `2, 1`).
4. Drop levels whose cols or rows fall below `min_coarsest`. If nothing remains (data fits within a single chunk), fall back to `(finest_cols, finest_rows)` directly.
5. Take the finest `max_lods` levels; assign LOD indices starting at 1 (coarsest).

Example: `Product._compute_lod_grids(3000, 1500, (256, 256))` → `{1: (3, 2), 2: (6, 3), 3: (12, 6)}`.

Small-dataset example (radar SA Gulfs, 102×74, chunk 240×192): finest=(1,1), filtered to nothing, fallback → `{1: (1, 1)}`.

### 7.3 Lazy population (`services/loader.py` — `get_lod_grids`)

Products start with `lod_grids={}`. On the first request:

1. `get_lod_grids(product)` checks `product.lod_grids` — empty, so proceeds (double-checked locking).
2. Opens the Zarr store (singleton — reused across all calls to the same URL).
3. Reads lat/lon dimension sizes from store metadata (`.zmetadata`, no data fetch).
4. Calls `product.apply_computed_lod_grids(data_width, data_height)`, which runs `_compute_lod_grids` and populates the result via `self.lod_grids.update()`. Although `Product` is a frozen dataclass, `lod_grids` is a mutable dict — `update()` mutates the dict in place without reassigning the attribute, so no frozen-bypass is needed.
5. All subsequent calls return immediately from the `if product.lod_grids` guard.

---

## 8. PNG encoding contract (data tiles)

Data tiles are RGBA PNGs (`optimize=False`). The byte layout is fixed and consumed by a WebGL shader:

- **24-bit scalar** (GSLA, SSTA, WDIR, etc.): R=high byte, G=mid byte, B=low byte of a normalised uint24; A=ocean mask (255=ocean, 0=land). Land pixels have RGB zeroed (premultiplied form).
- **UV vector** (e.g. ocean current): R=U normalised to 8-bit, G=V normalised to 8-bit, B=ocean mask × 255, A=255.

Normalisation ranges (`valueRange`, `uRange`/`vRange`) are computed from the full pre-resampled dataset and returned in `manifest.json`. All tiles for a date share the same ranges.

Visual tiles do **not** use this contract — they return ordinary colourised PNGs after applying a colormap LUT.

---

## 9. Visual renderer specifics (CRS guard and antimeridian)

`services/visual_renderer.py` uses rio-tiler's `XarrayReader`, which requires data in **EPSG:4326** (geographic lat/lon degrees) with bounds strictly within `(−180, −90, 180, 90)`.

### 9.1 CRS guard

`_to_scalar_parts` validates coordinate ranges before passing data to `XarrayReader`:

- `lat ∈ [−90, 90]`
- `lon ∈ [−180, 360]` (allows 0–360 convention before normalisation)

A dataset in a projected CRS (e.g. UTM, GDA94/MGA) would have coordinate values in the millions and is rejected immediately with a descriptive `ValueError`. This prevents silent mis-rendering — the hardcoded `write_crs("EPSG:4326")` call would otherwise label projected coordinates as geographic without error.

### 9.2 Antimeridian handling

Some stores use longitudes that extend past 180° (e.g. GSLA: 57–185°E). `XarrayReader` rejects any bounds outside `±180`, so these must be normalised. The approach depends on the data topology:

**Detection — contiguity check**: normalise all `lon > 180` to negative values (`lon − 360`), then sort. If the maximum gap between adjacent sorted values is ≤ 2× the native resolution, the data is a contiguous global-style grid and wrap-and-sort is safe. A large gap (e.g. 232° for GSLA) means the data is a regional window straddling the antimeridian.

**Global data (contiguous after normalisation)**: standard wrap-and-sort to `[−180, 180)`.

**Regional antimeridian straddle** (e.g. GSLA 57–185°E): the dataset is split into two segments:

| Segment | Lon range                   | Notes                           |
| ------- | --------------------------- | ------------------------------- |
| Primary | `lon < 180`                 | Native coords unchanged         |
| Minor   | `lon > 180` shifted by −360 | e.g. 180.2–185 → −179.8 to −175 |

`lon == 180` is excluded from both segments to keep each segment's half-pixel rioxarray bounds strictly inside `±180`.

Both segments are rendered independently using `XarrayReader` and the results are alpha-composited (non-transparent overlay pixels replace base pixels). Most tile/bbox requests intersect only one segment; the composite is a no-op for the non-intersecting segment. This ensures data near the antimeridian (e.g. the Tonga/Fiji strip for GSLA) is rendered correctly rather than silently dropped.

---

## 10. Date and timezone convention

**This is a critical invariant.** Getting it wrong causes silent 404s or data served for the wrong day.

### 10.1 The rule

| Layer                        | Representation                                                                        |
| ---------------------------- | ------------------------------------------------------------------------------------- |
| Zarr store `time` coordinate | UTC — numpy `datetime64[ns]` is always UTC by convention                              |
| API request/response dates   | Local time in `TILE_TIMEZONE` (default `Australia/Sydney`, AEST UTC+10 / AEDT UTC+11) |

`TILE_TIMEZONE` is an IANA timezone name read at startup. To deploy this server for a different region, set it in `.env` or `docker-compose.yml` before starting — no code changes needed. All date conversion (manifest output, tile request matching, error messages) uses the configured timezone automatically.

All satellite passes over Australia occur during Australian daytime. Their UTC timestamps typically fall on the **previous UTC day** (e.g. a pass at `2022-06-01 01:20 AEST` is `2022-05-31 15:20 UTC`). Comparing UTC dates to local request dates directly would return a 404 for every such record.

### 10.2 How the server handles it

`LOCAL_TZ` is read once at startup from the `TILE_TIMEZONE` environment variable in `utils/dates.py`:

```python
LOCAL_TZ = ZoneInfo(os.environ.get("TILE_TIMEZONE", "Australia/Sydney"))

def ts_to_local_date(ts) -> str:
    return str(pd.Timestamp(ts).tz_localize("UTC").tz_convert(LOCAL_TZ).strftime("%Y-%m-%d"))
```

Every point where a UTC timestamp is exposed or compared is converted via `ts_to_local_date`:

- **`get_available_dates`** — converts store timestamps to local date strings. The manifest always returns values the client can round-trip back unchanged as request dates.
- **`load_slice`** — iterates all timestamps in the store's `time` coordinate, converts each via `ts_to_local_date`, and collects those that match the requested date string exactly. The first matching timestamp is selected with `sel(time=pd.Timestamp(matching[0]))`. If multiple timestamps map to the same local date (e.g. sub-daily data), a warning is logged and the first is used. If no timestamp maps to the requested local date, `FileNotFoundError` is raised with a message indicating that dates must be in `LOCAL_TZ` local time (not UTC). This avoids `method="nearest"` silently serving data from an adjacent day.

**Critical constraint** — `get_available_dates` and `load_slice` must always use the same `LOCAL_TZ` value. Changing one without the other causes dates to silently mismatch: the manifest returns dates the client cannot successfully request. `TILE_TIMEZONE` is the single source of truth; never hardcode a timezone string in either function.

### 10.3 Client contract

Dates in the API are **opaque keys**, not calendar dates in the client's local timezone. Clients must:

1. Fetch available dates from `/manifest`.
2. Pass those exact date strings back in tile/point requests.

Do not construct date strings from the client's local clock — the server interprets them as `TILE_TIMEZONE` local dates, and a client in a different timezone would produce strings that do not exist in the manifest.

### 10.4 Sub-daily data

The current API is day-granularity only. If a store has sub-daily resolution, multiple UTC timestamps will map to the same local date — `load_slice` logs a warning and returns the first. Supporting hourly queries would require changes to the URL structure and cache-key design; deferred until there is a concrete use case.

---

## 11. Coordinate normalisation

On store open, `_open_store` in `services/loader.py` applies `COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}` to rename any uppercase coordinate names to lowercase. This happens once per store URL and is cached on the singleton. All downstream code (renderer, manifest, point endpoint) can assume `lat`/`lon`/`time` regardless of what the store uses natively.

If `lat`/`lon` are still missing after renaming, `_open_store` raises `ValueError` with a clear message rather than failing deeper in the pipeline.

---

## 12. Caching strategy

Three-tier cache stack ordered tiles → S3: **L1 (processed grid) → L2 (in-memory slice) → L3 (disk) → S3**. Visual tiles have no L1 — requests hit L2 first. Cold S3 reads (~2s) are absorbed by disk (L3, ~30ms) and in-memory LRU (L2, <1ms). The disk cache is the primary mechanism for eliminating cold origin hits — it persists across server restarts and is pre-populated at startup.

> Full design rationale (why disk over Redis / EFS / Fargate ephemeral): [`docs/cache_analysis.md`](cache_analysis.md).

### 12.1 Store singleton (`services/loader.py`, `_stores`)

Caches the open Zarr store handle (lazy, metadata only). Shared across all products that point at the same store URL.

Uses a **stale-while-revalidate** strategy to pick up newly appended time steps without ever blocking a request:

- **Startup** — `prewarm_stores` opens every registered store in background daemon threads so the cache is warm before the first request arrives.
- **Within TTL** — the cached store is returned immediately (sub-millisecond).
- **After TTL** (`STORE_TTL_SECONDS`, default `600`) — the stale store is returned immediately for the current request, and a single background daemon thread calls `_refresh_store_background` to re-open it. `_store_refreshing` prevents duplicate refresh threads for the same URL.
- **First-ever open** — the request blocks until `xr.open_zarr` completes; concurrent requests for the same URL wait on the same `concurrent.futures.Future` rather than each opening independently. The Future is keyed per-URL in `_store_in_flight`, so opens of _different_ URLs proceed in parallel.

Re-opening is cheap — `xr.open_zarr` reads only metadata and coordinate arrays (`time`, `lat`, `lon`), no data chunks. In-flight `load_slice` calls hold a direct Python reference to the old dataset object and complete normally. `_slice_cache` and `_processed_cache` entries for existing dates remain valid and unaffected.

### 12.2 L1 — Processed grid cache (`services/data_renderer.py`, `_processed_cache`)

Keyed `(source_path, date, str(variable), lod)`. Stores the resampled + normalised numpy arrays for the **full LOD grid**, not per-tile. A hit reduces per-tile work to `_extract_chunk` + PNG encode only — no S3 I/O, no resampling. The key is semantic (not object identity), so cache hits survive L2 slice evictions and disk-reloaded slices.

Entry sizes for the satellite heatwave product (2000×3900): LOD 1 ~1.4 MB, LOD 2 ~3.3 MB, LOD 3 ~12 MB, LOD 4 ~41 MB. GSLA and radar products have only 1 LOD level at ~1.4 MB.

Size is controlled by `PROCESSED_CACHE_SIZE` (default `50`). Sized as `SLICE_CACHE_SIZE × LOD.max_lods` with headroom: `10 × 4 = 40`, rounded to 50. This keeps all LOD levels warm for every date in the L2 slice cache.

On product deletion, `evict_processed_cache` is called from `evict_product_cache` in `loader.py` to purge all entries matching `source_path`.

Visual tiles do not use L1 — `XarrayReader` handles its own rendering per request from the L2 slice.

### 12.3 L2 — Slice cache, in-memory (`services/loader.py`, `_slice_cache`)

Keyed `(store_url, date, variables_tuple)`. Stores a fully-computed (`.compute()`) 2-D lat×lon `xr.Dataset` slice. Sub-millisecond on hit. Keyed by `variables_tuple` so different products using the same store cache independently.

Size is controlled by `SLICE_CACHE_SIZE` (default `10`). Entry size varies significantly by product: ~2 MB for GSLA (351×641), ~61 MB for the satellite heatwave products (2000×3900 float64).

Primary consumers are **visual_tiles** (no L1 above it — every tile request calls `load_slice`) and **data_tiles manifest/point** (always need `ds` directly). For data_tiles tile requests, the slice is only loaded on an L1 miss; once the processed grid is warm, L2 is bypassed entirely.

### 12.4 L3 — Slice cache, disk (`DISK_CACHE_PATH` directory)

Persists fully-computed slices as lz4-compressed pickles to survive server restarts. On an L2 miss, `load_slice` checks disk before going to S3. A disk hit (~30ms read + decompress) is ~60× faster than a cold S3 fetch.

File layout: `{DISK_CACHE_PATH}/{store_name}-{var_str}/{date}.pkl.lz4`.

Enabled by setting `DISK_CACHE_PATH` (e.g. `/app/slice_cache`). If unset, disk caching is disabled and all cold reads go directly to S3.

**Eviction:**

- _Stale dates_ — `evict_stale_and_orphans` (and the refresh cycle) deletes `.pkl.lz4` files whose dates are no longer in the `CACHE_DAYS` window for any registered product.
- _Orphan product directories_ — `evict_stale_and_orphans` removes any sub-directory under `DISK_CACHE_PATH` whose name does not correspond to a currently-registered product. This is what makes runtime product deletion safe: leftover disk slices from removed products are cleaned up automatically on the next refresh (and at startup).
- _Disk pressure_ — `_evict_disk_if_needed` (run at the start of each refresh cycle) removes files when total usage exceeds `DISK_EVICTION_THRESHOLD × DISK_CACHE_LIMIT_GB`. Files are sorted `(size ascending, date ascending)` — small + old files are evicted first, keeping the large satellite slices that would be most expensive to re-fetch.
- _Explicit product deletion_ — `evict_product_cache` (called by `DELETE /admin/products/{id}`) removes the product's disk directory via `shutil.rmtree` and purges matching entries from the L2 in-memory cache immediately.

Disk eviction never invalidates L2 in-memory entries — the in-memory data is still valid and serves requests until it falls out of the LRU naturally.

### 12.5 Stampede protection

All three layers use `concurrent.futures.Future` to deduplicate concurrent misses on the same key:

- `_store_in_flight` — store opens.
- `_slice_in_flight` — slice loads (`load_slice`).
- `_processed_inflight` — processed grid computation (`_get_processed`).

The first thread to miss the cache creates the Future and does the work; all other threads arriving for the same key block on `future.result()` and receive the same result when the single computation completes. Errors propagate to all waiting threads so a failed request does not permanently block future attempts for the same key. See [`docs/concurrency.md`](concurrency.md) for capacity implications.

---

## 13. Background tasks

The server runs two long-lived background tasks scheduled on the event loop at startup, plus several ad-hoc background actions. None of them block request handling — see [§14](#14-concurrency-event-loop-and-threading) for why.

### 13.1 Lifespan overview (`main.py`)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = int(os.environ.get("THREAD_POOL_SIZE", 100))

    load_products()                      # sync: read products.json into PRODUCTS dict
    load_colormaps()                     # sync: read colormaps.json into the colormap registry

    store_urls = list({p.source_path for p in PRODUCTS.values()})
    prewarm_stores(store_urls)           # spawns one daemon thread per unique store URL

    prewarm_task = asyncio.create_task(_startup_cache_sync(list(PRODUCTS.values())))
    interval = int(os.environ.get("CACHE_REFRESH_INTERVAL_SECONDS", 14400))
    refresh_task = asyncio.create_task(_cache_refresh_loop(interval))

    yield  # ← server handles requests here

    for task in (prewarm_task, refresh_task):
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
```

Everything before `yield` runs on startup; everything after runs on shutdown. **The server begins handling requests immediately at `yield`** — it does not wait for prewarm or refresh to finish. Both background tasks pause at `await` points so the event loop is free for incoming requests.

### 13.2 `prewarm_task` — startup cache sync (one-shot)

Wraps `_startup_cache_sync(products)`, which does two sequential phases off the event loop via `asyncio.to_thread`:

1. **`evict_stale_and_orphans(products)`** — removes
   - any cached `.pkl.lz4` files for dates outside the `CACHE_DAYS` window for each product, **and**
   - any cache sub-directory whose name doesn't match a currently registered product (orphans left over after a product was removed in a previous run).

   This ensures the disk cache reflects the current product/date state from the moment the server starts serving, not just after the first refresh cycle.

2. **`prewarm_disk_slices(products)`** — for each `(product, date)` pair in the last `CACHE_DAYS` dates, calls `load_slice`. Disk-cached pairs return instantly; missing ones are fetched from S3 and written to disk. Parallelised across `PREWARM_WORKERS` workers (default `4`) using a `ThreadPoolExecutor`. The pool's `__exit__` calls `shutdown(wait=True)` so the function returns only after every job has finished.

If eviction fails for any reason it is logged and prewarm proceeds anyway — partial cache is better than no cache.

The task completes once; it is not periodic.

### 13.3 `refresh_task` — periodic cache refresh (long-running)

`_cache_refresh_loop(interval)` runs in an infinite `while True` loop:

```python
async def _cache_refresh_loop(interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(refresh_disk_cache, list(PRODUCTS.values()))
        except Exception:
            logger.exception("Cache refresh cycle failed; will retry next interval")
```

Key properties:

- **Re-reads `PRODUCTS` every cycle** — products added or removed via the admin API are picked up automatically on the next tick; no restart required.
- **Broad exception handling** — an unhandled exception inside the loop would kill it for the lifetime of the process and silently disable all future refreshes. The broad `except` keeps the loop alive across transient failures.
- **`asyncio.sleep` yields to the event loop** — other tasks and requests run freely during the wait.
- **`asyncio.to_thread` for the heavy work** — `refresh_disk_cache` does disk I/O, S3 fetches, and `.compute()` calls; running it inline on the event loop would freeze the server for tens of seconds.

`refresh_disk_cache` itself:

1. Calls `evict_stale_and_orphans(products)` first — drops files outside the date window and removes orphan product directories.
2. For each product, computes the target window (`get_available_dates[-CACHE_DAYS:]`) and writes any missing date's slice to disk via `load_slice` + `lz4.frame.compress(pickle.dumps(ds))`.

Default interval is `CACHE_REFRESH_INTERVAL_SECONDS = 14400` (4 hours). In steady state on IMOS daily data this adds ~1 new date per product per cycle and evicts ~1 stale date.

### 13.4 Other background actions

| Trigger                     | Action                                                                                       | Mechanism                                                                |
| --------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `prewarm_stores` at startup | Open each unique Zarr store URL (metadata only) so first requests don't pay the cost         | One `threading.Thread(daemon=True)` per URL                              |
| Store TTL expiry            | Re-open Zarr store in the background to pick up new timestamps; stale store served meanwhile | `_refresh_store_background` via `threading.Thread`                       |
| `POST /admin/products`      | Prewarm the disk cache for the newly registered product                                      | `asyncio.create_task(asyncio.to_thread(prewarm_disk_slices, [product]))` |
| `DELETE /admin/products`    | Evict the product's in-memory L1/L2 entries and remove its disk directory                    | Synchronous on the request thread (fast — file delete + dict pop)        |

### 13.5 Graceful shutdown

On shutdown (Uvicorn signal handler), the lifespan `finally` block:

- `cancel()`s both `prewarm_task` and `refresh_task`.
- `await`s each one to handle `asyncio.CancelledError` cleanly.
- Logs any other exception that escaped.

Daemon threads (store prewarm, store TTL refresh, disk-prewarm executor inside `prewarm_disk_slices` once it has returned) do not need explicit cleanup — they exit with the process.

---

## 14. Concurrency: event loop and threading

The server combines an **asyncio event loop** (for FastAPI/Uvicorn request multiplexing and the two long-lived background tasks) with a **bounded thread pool** (for all CPU- and I/O-heavy work). Understanding which work runs where is essential when reasoning about latency, throughput, and capacity.

### 14.1 Why most endpoints are `def`, not `async def`

Look at the route definitions in `routers/`:

```python
@router.get("/{product_id}/{date}/tiles/{z}/{x}/{y}.png")
def get_tile(...):
    ...
```

These are **synchronous** `def` functions, not `async def`. FastAPI/Starlette inspects each handler at registration time and routes sync handlers to a thread pool managed by `anyio` (the same `anyio.to_thread.current_default_thread_limiter()` whose `total_tokens` we set to `THREAD_POOL_SIZE` in the lifespan).

The reason is twofold:

1. **`xarray` / `zarr` / `rio-tiler` are blocking libraries.** None of them expose async read APIs. A call to `ds.sel(...).compute()` blocks until the S3 chunks are downloaded and decompressed; a call to `XarrayReader.tile(...)` blocks until reprojection finishes. If we wrote these handlers as `async def`, every blocking call would freeze the event loop — every request would queue up behind whichever one happened to be fetching from S3 (potentially seconds).
2. **PNG encoding, numpy resampling, and lz4 decompression are CPU-bound.** Even ignoring I/O, the actual work per tile is non-trivial (a satellite LOD-4 grid is 41 MB to allocate, normalise, and pack). Doing that on the event loop would block every other request for the duration.

By defining handlers as plain `def`, each one runs on a worker thread from the anyio pool. The event loop stays responsive: it only does the work of accepting connections, parsing HTTP headers, dispatching to handlers, and serialising responses.

### 14.2 The thread pool

```python
limiter = anyio.to_thread.current_default_thread_limiter()
limiter.total_tokens = int(os.environ.get("THREAD_POOL_SIZE", 100))
```

The pool has `THREAD_POOL_SIZE` slots (default 100). Each in-flight sync request occupies one slot from the start of the handler to its return. The Python GIL means only one thread executes CPU-bound Python at a time, but:

- **I/O releases the GIL** — `xarray`'s S3 fetch is mostly `urllib3`/`botocore` socket I/O. While one thread waits on S3, others can run.
- **numpy/PIL release the GIL during their C-level work** — resampling, normalisation, and PNG encoding all benefit from real parallelism.

Stampede protection (`_slice_in_flight`, `_processed_inflight`, `_store_in_flight`) means that if 10 requests arrive for the same cold key, only 1 thread does the work; the other 9 hold their slots blocked on the Future. This caps peak unique work and peak RAM, but the held slots do count toward `THREAD_POOL_SIZE`. See [`docs/concurrency.md`](concurrency.md) for the full capacity analysis.

### 14.3 Background tasks run on the event loop and offload work via `asyncio.to_thread`

The two `asyncio.create_task(...)` calls in `lifespan` create coroutines that run on the event loop:

- `_startup_cache_sync` — awaits `asyncio.to_thread(evict_stale_and_orphans, products)`, then `asyncio.to_thread(prewarm_disk_slices, products)`.
- `_cache_refresh_loop` — awaits `asyncio.sleep(interval)`, then `asyncio.to_thread(refresh_disk_cache, ...)`.

Each `await` is a yield point: the event loop is free to dispatch other tasks (including incoming HTTP requests) until the awaited operation completes. The blocking work itself (S3 fetches, disk reads, `.compute()`) runs on a thread from the pool — it does **not** run on the event loop.

This is why a 60-second prewarm at startup does not delay the first request by 60 seconds. The event loop yields at the `await asyncio.to_thread(...)` boundary, the prewarm threads run in the background, and the event loop continues to handle requests on other threads.

`prewarm_disk_slices` itself further parallelises across `(product, date)` pairs using `concurrent.futures.ThreadPoolExecutor(max_workers=PREWARM_WORKERS)` — those workers are _separate_ from the anyio request pool. They share CPU and S3 bandwidth but not slot accounting.

### 14.4 Quick reference

| Component                         | Runs on                                           | Why                                                                        |
| --------------------------------- | ------------------------------------------------- | -------------------------------------------------------------------------- |
| HTTP accept / parse / route       | Event loop                                        | Pure async I/O; never blocks                                               |
| Tile/manifest/point handlers      | Anyio thread pool (`THREAD_POOL_SIZE`)            | Sync `def` so blocking xarray/rio-tiler/PIL calls don't freeze the loop    |
| `/admin/products` POST/DELETE     | Event loop (`async def`)                          | Fast JSON read/write only; admin product prewarm offloaded via `to_thread` |
| `/products`, `/colormaps` listing | Event loop (`async def`)                          | In-memory dict reads only                                                  |
| Store prewarm at startup          | One daemon thread per URL                         | Fire-and-forget metadata fetch                                             |
| Store TTL refresh                 | One daemon thread per URL on TTL expiry           | Stale store returned immediately; fresh open happens in the background     |
| `_startup_cache_sync`             | Event-loop task → `asyncio.to_thread`             | Coroutine yields while the actual eviction + prewarm work runs on threads  |
| `_cache_refresh_loop`             | Event-loop task → `asyncio.to_thread`             | Coroutine yields during sleep + refresh; never blocks the loop             |
| `prewarm_disk_slices` parallelism | Internal `ThreadPoolExecutor` (`PREWARM_WORKERS`) | Parallel S3 fetches independent of request thread pool                     |
| In-flight stampede dedup          | Anyio thread pool (callers block on `Future`)     | Holds a slot but does no work — see §14.2                                  |

### 14.5 Failure modes to watch

- **`async def` an endpoint by accident.** If a future contributor turns a `def` handler into `async def`, blocking calls inside it (any `xarray`/`rio-tiler` call) will freeze the event loop and serialise every request behind the slowest one. There is no static check for this — review carefully.
- **Forget `asyncio.to_thread` inside a background task.** A future addition like `await some_sync_function()` would suspend the coroutine forever (no awaitable) or, worse, run the sync function inline on the event loop. Anything CPU/IO-heavy must be wrapped in `asyncio.to_thread`.
- **Unbounded background tasks.** Both lifespan tasks have a top-level `try/except`. New background tasks must do the same — an unhandled exception in an `asyncio.Task` is silent until the task is awaited.

---

## 15. Capacity and resource planning

This section quantifies how RAM and disk grow with product count, slice size, thread-pool size, and cache size. Use it when picking instance class for a new deployment or sizing a horizontal scale-out.

### 15.1 Production product mix (planning baseline)

The development environment contains four products of mixed sizes (including a small radar product). **Production is different and is the basis for all numbers below:**

| Class                          | Grid                | Vars | Raw / date | lz4 / date | Expected count in production |
| ------------------------------ | ------------------- | ---- | ---------- | ---------- | ---------------------------- |
| Satellite-class (e.g. SSTA)    | 2000 × 3900 float64 | 1    | **~61 MB** | **~18 MB** | **≥ 10**                     |
| Sea-level-anomaly class (GSLA) | 351 × 641 float64   | 1    | ~1.7 MB    | ~0.5 MB    | 1                            |
| Ocean-current class            | 351 × 641 float64   | 2    | ~3.4 MB    | ~1.0 MB    | 1                            |
| Small radar                    | 74 × 102 float64    | 1    | ~0.06 MB   | ~0.02 MB   | **0** (dev-only)             |

For all planning, treat the working set as **10–20 satellite-class products plus two small products**, with `CACHE_DAYS` set anywhere from 30 up to **90 (3 months)**. Memory and disk are dominated by satellite slices; the two small products contribute < 1 % of total footprint and are ignored in the totals below.

### 15.2 RAM components

| Component                             | Sizing rule                                                                                                                    | Magnitude with N satellite products in production                   |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------- |
| Process baseline                      | Python + FastAPI + xarray + numpy + rio-tiler + PIL                                                                            | ~250–350 MB                                                         |
| Store singletons                      | One open `xr.Dataset` per unique URL — metadata + coord arrays only, no data chunks                                            | ~5 MB × stores ≈ tens of MB                                         |
| L2 slice cache                        | `SLICE_CACHE_SIZE × 61 MB` (every satellite slot ≈ 61 MB)                                                                      | Grows linearly with `SLICE_CACHE_SIZE`                              |
| L1 processed grid cache               | `PROCESSED_CACHE_SIZE × per-entry size`. Per-entry by LOD: 1.4 / 3.3 / 12 / 41 MB. All 4 LODs of one (product, date) = ~58 MB. | Grows linearly with `PROCESSED_CACHE_SIZE`                          |
| In-flight slices (transient)          | `unique_cold_keys × 61 MB` (stampede-dedup'd; not `THREAD_POOL_SIZE × slice_size`)                                             | Up to a few hundred MB to several GB peak under cold-traffic bursts |
| In-flight processed grids (transient) | `unique_keys × grid_size` (LOD-4 = 41 MB)                                                                                      | Hundreds of MB peak                                                 |

**Cache RAM as a function of cache size, assuming satellite-dominated slots:**

| `SLICE_CACHE_SIZE` | `PROCESSED_CACHE_SIZE` | L2 worst-case | L1 worst-case (LOD-4 only) | L1 mixed-LOD typical | Steady RAM (baseline + L2 + L1) |
| -----------------: | ---------------------: | ------------: | -------------------------: | -------------------: | ------------------------------: |
|                 10 |                     50 |       ~610 MB |                    ~2.0 GB |              ~750 MB |                         ~1.7 GB |
|                 20 |                     80 |       ~1.2 GB |                    ~3.2 GB |              ~1.2 GB |                         ~2.7 GB |
|                 30 |                    120 |       ~1.8 GB |                    ~4.8 GB |              ~1.8 GB |                         ~3.9 GB |
|                 60 |                    240 |       ~3.7 GB |                    ~9.6 GB |              ~3.6 GB |                         ~7.6 GB |
|                100 |                    400 |       ~6.1 GB |                   ~16.0 GB |              ~6.0 GB |                        ~12.4 GB |

"L1 mixed-LOD typical" assumes a realistic distribution across the four LOD levels (most cache slots are _not_ LOD 4). "Steady RAM" uses mixed-LOD plus ~350 MB baseline. Add ~500 MB–2 GB transient headroom for in-flight cold loads.

### 15.3 Why the default `SLICE_CACHE_SIZE=10` is too small for production

With **10+ satellite products**, default `SLICE_CACHE_SIZE=10` gives you at most one cache slot per product. Any request for a non-cached date evicts another product's most recent slice — the cache thrashes and most visual-tile requests fall through to disk (or S3 on cold start). Two sizing principles:

- **At minimum**, size for one slot per product: `SLICE_CACHE_SIZE ≥ product_count`. With 10 satellite products that means **`SLICE_CACHE_SIZE = 10`** is the _floor_, not the recommended setting.
- **Recommended**, size for a few recent dates per product so users panning across recent dates stay in L2: `SLICE_CACHE_SIZE ≈ product_count × hot_dates_per_product`. For 10 products with ~3 hot dates each: **`SLICE_CACHE_SIZE = 30`**, **`PROCESSED_CACHE_SIZE = 120`** (i.e. `SLICE_CACHE_SIZE × LOD.max_lods`).

Memory cost of these recommendations: ~2.7 GB and ~3.9 GB steady respectively, before transient headroom. CloudFront mitigates the visible impact of L2 misses for repeat tile URLs but does not help requests for new dates.

### 15.4 How RAM scales when products are added

| Change                                                        | RAM impact                                                                                                                              |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Add a satellite-class product without changing cache sizes    | No new RAM ceiling, but L2/L1 hit rates degrade — more products compete for the same slots, more cold S3 reads.                         |
| Add a satellite-class product and raise `SLICE_CACHE_SIZE +1` | + ~61 MB L2, + ~58 MB L1 (one full row of LODs).                                                                                        |
| Raise `SLICE_CACHE_SIZE` by N (satellite worst case)          | + `N × 61 MB` L2, + `N × ~58 MB` L1.                                                                                                    |
| Raise `THREAD_POOL_SIZE`                                      | No direct steady RAM growth (~1 MB stack/thread). Higher _unique_ concurrent cold misses can spike transient RAM by `(N_cold) × 61 MB`. |
| Raise `PREWARM_WORKERS`                                       | Startup-only spike of `PREWARM_WORKERS × 61 MB`. Default 4 ≈ 250 MB.                                                                    |
| Raise `CACHE_DAYS` (e.g. 30 → 90)                             | **No effect on RAM** — only affects disk. L2/L1 sizes are bounded by their LRU sizes regardless of how many dates are on disk.          |

Stampede protection (`_slice_in_flight`, `_processed_inflight`) means transient RAM scales with **unique cold keys in flight**, not `THREAD_POOL_SIZE`. But under truly mixed cold traffic (different `(product, date)` pairs from many users at once), the cap is `min(THREAD_POOL_SIZE, distinct_keys) × 61 MB`. With `THREAD_POOL_SIZE = 100` and a perfect-storm spread across many products and dates, that ceiling is **~6 GB** — short-lived but real. Provision RAM accordingly or lower `THREAD_POOL_SIZE`.

### 15.5 Thread pool vs cache sizing

Thread-pool size and cache size are **independent knobs**:

| Goal                                                 | Knob                                                                         |
| ---------------------------------------------------- | ---------------------------------------------------------------------------- |
| Serve more concurrent requests without queueing      | Raise `THREAD_POOL_SIZE` (cheap in steady RAM; raises transient ceiling)     |
| Keep more `(product, date)` pairs hot                | Raise `SLICE_CACHE_SIZE` (and `PROCESSED_CACHE_SIZE = SLICE_CACHE_SIZE × 4`) |
| Keep more dates available without S3 on warm restart | Raise `CACHE_DAYS` (affects disk, not RAM)                                   |
| Shorten startup duration                             | Raise `PREWARM_WORKERS`                                                      |

Recommended pairings for the production product mix:

| Production mix                       | `SLICE_CACHE_SIZE` | `PROCESSED_CACHE_SIZE` | `THREAD_POOL_SIZE` | Steady RAM (cache+base) | Peak transient |
| ------------------------------------ | ------------------ | ---------------------- | ------------------ | ----------------------- | -------------- |
| 10 satellite + 2 small (1 hot date)  | 12                 | 50                     | 100                | ~2 GB                   | +~6 GB worst   |
| 10 satellite + 2 small (3 hot dates) | 30                 | 120                    | 100                | ~4 GB                   | +~6 GB worst   |
| 20 satellite + 2 small (1 hot date)  | 22                 | 90                     | 100                | ~3 GB                   | +~6 GB worst   |
| 20 satellite + 2 small (3 hot dates) | 60                 | 240                    | 100                | ~7.6 GB                 | +~6 GB worst   |

"Peak transient" assumes a worst-case burst of unique cold satellite slices arriving simultaneously. In practice CloudFront and stampede dedup keep this much lower; the column exists to right-size RAM with headroom rather than predict steady use.

### 15.6 Disk usage (L3 cache)

Per-product per-date footprint after lz4 compression is dominated by satellite products: **~18 MB per date per satellite product**. The two small products contribute < 1 MB per date combined and are ignored below.

**Approximate disk total = `N_satellite × CACHE_DAYS × 18 MB`:**

| Satellite products | `CACHE_DAYS = 30` | `CACHE_DAYS = 60` | `CACHE_DAYS = 90` |
| -----------------: | ----------------: | ----------------: | ----------------: |
|                 10 |             ~5 GB |            ~11 GB |        **~16 GB** |
|                 15 |             ~8 GB |            ~16 GB |            ~24 GB |
|                 20 |            ~11 GB |            ~21 GB |            ~32 GB |
|                 30 |            ~16 GB |            ~32 GB |            ~49 GB |

`DISK_CACHE_LIMIT_GB` (default 20) + `DISK_EVICTION_THRESHOLD` (default 0.85) means pressure eviction begins at 17 GB used. **At the planning baseline of 10 satellite products and `CACHE_DAYS = 90`, you are already at ~16 GB — within margin of default eviction.** Anything beyond 10 products _or_ 90 days requires raising `DISK_CACHE_LIMIT_GB` (and the underlying EBS volume) explicitly.

Files are evicted by `(size ascending, date ascending)`. With a uniform satellite-class fleet, size order ceases to be useful — eviction effectively becomes oldest-date-first across all products, which is what you want.

**Recommended sizing:** plan for at least 1.5× the steady-state disk total to cover transient writes during refresh and uneven product growth.

| Production target     | Steady disk | Recommended `DISK_CACHE_LIMIT_GB` | Recommended EBS volume |
| --------------------- | ----------: | --------------------------------: | ---------------------- |
| 10 satellite, 30 days |       ~5 GB |                                 8 | 16 GB gp3              |
| 10 satellite, 90 days |      ~16 GB |                                24 | 32 GB gp3              |
| 20 satellite, 90 days |      ~32 GB |                                48 | 64 GB gp3              |
| 30 satellite, 90 days |      ~49 GB |                                72 | 100 GB gp3             |

EBS gp3 costs $0.08/GB-month, so even the 30-product / 90-day deployment is ~$8/month for storage — disk is not a cost lever, capacity-planning correctness is.

### 15.7 Worked example — picking instance size for 10 satellite products, 90-day window

Target: **10 satellite-class products + sea_level_anomaly + ocean_current**, `CACHE_DAYS = 90`, expected sustained ~50 concurrent requests, fronted by CloudFront.

- **Disk:** 10 × 90 × 18 MB ≈ 16 GB plus negligible small-product overhead. Set **`DISK_CACHE_LIMIT_GB = 24`** (eviction at ~20 GB, leaves headroom) on a 32 GB gp3 volume.
- **Cache sizing:** to keep all 10 satellite products' three most recent dates warm, use **`SLICE_CACHE_SIZE = 30`**, **`PROCESSED_CACHE_SIZE = 120`**.
- **Steady RAM:**
  ```
    350 MB baseline
  +  50 MB store singletons
  + 1.8 GB L2 (30 × 61 MB worst case)
  + 1.8 GB L1 (mixed-LOD ~60 MB average × 30 product/date rows)
  = ~4 GB steady
  ```
- **Peak RAM (cold-burst transient):** add up to ~6 GB worst-case transient with `THREAD_POOL_SIZE = 100` (rare in practice — CloudFront absorbs warm traffic, stampede dedup absorbs simultaneous duplicates).
- **Plan for ≥ 8 GB RAM, 16 GB recommended** to absorb cold bursts comfortably. **`m6i.xlarge`** (4 vCPU, 16 GB) is a good fit; **`m6i.large`** (2 vCPU, 8 GB) is feasible only if you accept that a worst-case cold burst will swap or OOM. Lower `THREAD_POOL_SIZE` to 50 if forced onto `m6i.large`.
- **CPU:** request handling is mostly I/O-bound (S3 + disk) with bursts of numpy/PIL work. 4 vCPU is sufficient; 8 vCPU helps only under sustained cold-traffic bursts.
- **Prewarm:** 10 × 90 = 900 jobs at `PREWARM_WORKERS = 4` is ~10–15 minutes on cold start (S3 fetch + decompress + pickle + lz4 + write). Raise `PREWARM_WORKERS` to 8 for ~half the startup window at the cost of double the startup transient RAM (~500 MB). On warm restart (disk already populated), prewarm completes in seconds.

**Scaling guide:**

| Target                      | Instance class                                | Cache settings        | `DISK_CACHE_LIMIT_GB` |
| --------------------------- | --------------------------------------------- | --------------------- | --------------------- |
| 10 satellite, 30-day window | `m6i.large` (8 GB)                            | `SLICE=12 / PROC=50`  | 8                     |
| 10 satellite, 90-day window | `m6i.xlarge` (16 GB)                          | `SLICE=30 / PROC=120` | 24                    |
| 20 satellite, 90-day window | `m6i.2xlarge` (32 GB)                         | `SLICE=60 / PROC=240` | 48                    |
| 30 satellite, 90-day window | `m6i.4xlarge` (64 GB) or horizontal scale-out | `SLICE=90 / PROC=360` | 72                    |

Horizontal scale-out is straightforward — each replica has its own L1/L2/L3 caches but reads from the same S3 stores; CloudFront fans out at the edge. Past ~30 products it is usually cheaper to add a second smaller node than to grow a single instance.

> Full capacity-per-request-type tables (hot/disk-warm/cold throughput per request) are in [`docs/concurrency.md`](concurrency.md).

---

## 16. Colormap system

Visual tiles support any colormap name that resolves through the following lookup chain (first match wins):

1. **Custom registry** (`colormaps.json`) — names registered at runtime via the admin API.
2. **rio-tiler built-ins** — e.g. `viridis`, `plasma`, `inferno`.
3. **matplotlib** — any name from `matplotlib.colormaps`, including diverging maps like `RdBu_r`, `coolwarm`.

An unrecognised name returns `400 Bad Request`.

### 16.1 Listing supported colormaps

`GET /visual_tiles/colormaps` returns all supported names grouped by source, with higher-priority sources excluding duplicate names from lower ones:

```json
{
  "custom": [{ "name": "imos_sst", "mode": "ramp" }],
  "rio_tiler": ["accent", "algae", "viridis", "..."],
  "matplotlib": ["Blues", "RdBu_r", "coolwarm", "..."]
}
```

### 16.2 Custom colormaps

Registered via `POST /admin/colormaps` and persisted in `colormaps.json`. Loaded on startup by `load_colormaps()` in `services/colormap_store.py` and take effect immediately without a server restart. All colormap state lives in `colormap_store.py` — no other module holds it directly.

All colormaps are stored internally as **256-entry RGBA LUTs** (one tuple per normalised byte value, where 0 = data minimum and 255 = data maximum after `rescale`). The `POST /admin/colormaps` payload normalises the input to this format at registration time.

### 16.3 Colormap modes

The `mode` field on `POST /admin/colormaps` controls how the input stops are expanded to the 256-entry LUT:

| Mode             | `entries` format              | Behaviour                                                                |
| ---------------- | ----------------------------- | ------------------------------------------------------------------------ |
| `ramp` (default) | 2–256 colour stops            | Evenly-spaced stops linearly interpolated to 256 entries                 |
| `categorical`    | dict `{"<int>": colour, ...}` | Each integer value maps to one LUT slot; all other slots are transparent |

Each colour stop (in both modes) may be a CSS hex string (`#rgb`, `#rrggbb`, `#rrggbbaa`) or a `[r, g, b, a]` list. Hex strings without alpha default to fully opaque (a=255).

**Ramp example** — 5 stops interpolated across the full LUT:

```json
{
  "name": "ocean_depth",
  "mode": "ramp",
  "entries": ["#000080", "#00ffff", "#ffffff", "#ff8c00", "#8b0000"]
}
```

**Categorical example** — discrete class values 1–4:

```json
{
  "name": "land_cover",
  "mode": "categorical",
  "entries": { "1": "#ffff00", "2": "#0000ff", "3": "#ff0000", "4": "#000000" }
}
```

For categorical colormaps, `rescale=min,max` is **required** at render time and must match the range of the integer keys (e.g. `?rescale=1,4`). Omitting `rescale` with a categorical colormap returns `400 Bad Request`. This is enforced because the renderer auto-rescales to the per-tile data range when `rescale` is absent, which would corrupt the LUT slot mapping.

The data range for a categorical colormap is inferred from the key range (`min(keys)` → `max(keys)`) at registration time and used to place each value in the LUT. Values not covered by any key render as fully transparent.

### 16.4 Categorical colormaps are dataset-specific

A categorical colormap is tightly coupled to a specific variable's integer encoding — equivalent to the CF convention `flag_values` + `flag_colors` pair that ncWMS reads from dataset attributes. The `entries` keys must exactly match the discrete integer values that appear in the dataset.

**The server does not validate this coupling.** The colormap and product are registered independently, so applying a categorical colormap to a dataset with a different value encoding will render without error but produce silently wrong colours. For example, a colormap registered with keys `{1, 2, 3, 4}` applied to a dataset whose actual values are `{0, 1, 2, 3}` will shift every colour by one slot.

Practical rules:

- One categorical colormap = one dataset variable encoding. Do not reuse a categorical colormap across products unless they share the exact same integer values.
- Name categorical colormaps after the dataset or variable they describe (e.g. `land_cover_classes`, `ocean_current_flag`) to make the coupling explicit.
- Ramp colormaps are dataset-agnostic; categorical colormaps are not.

### 16.5 Cache behaviour

`_colormap()` in `services/visual_renderer.py` is `@lru_cache`-d (max 64 entries). The cache is cleared automatically whenever a colormap is added or deleted via the admin API — `colormap_store._reload()` calls `_colormap.cache_clear()` after every write.

---

## 17. Adding a new product

Products are managed at runtime via the admin API — no code changes or redeploy required. All products are persisted in `products.json` (the single source of truth) and loaded into the `PRODUCTS` dict on startup.

### 17.1 Via admin API

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

On registration:

- `products.json` is written and `PRODUCTS` is reloaded.
- `evict_product_cache` is **not** called for new products (nothing to evict).
- A disk-cache prewarm for the new product is fired with `asyncio.create_task(asyncio.to_thread(prewarm_disk_slices, [product]))`.

On the first request after registration:

- The store is opened and coordinates are normalised automatically.
- LOD grids are computed from the store's actual lat/lon dimensions (see [§7](#7-lod-grid-system)).
- Rendering and manifest generation work generically from `product.variable`.

On deletion:

- `products.json` is rewritten without the product, and `PRODUCTS` reloads.
- `evict_product_cache` removes the product's entries from `_slice_cache`, the L1 processed cache, and its disk directory.

### 17.2 Requirements for the Zarr store

| Requirement        | Detail                                                                                                                                                                                                  |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Coordinate names   | Must be `lat`/`lon`/`time`, or the uppercase variants `LATITUDE`/`LONGITUDE`/`TIME` (renamed automatically on open). If a store uses different names, add a mapping to `COORD_NAMES` in `constants.py`. |
| Spatial dimensions | `lat` and `lon` must be present after normalisation — `_open_store` raises `ValueError` with a clear message if not.                                                                                    |
| CRS                | Coordinates must be geographic degrees (EPSG:4326). The visual renderer guards against projected CRS values; see [§9.1](#91-crs-guard).                                                                 |
| Variable           | The variable(s) named in `Product.variable` must exist in the store.                                                                                                                                    |

### 17.3 Optional overrides

`Product` fields can be customised per product if the defaults don't fit:

| Field       | Default              | When to override                                          |
| ----------- | -------------------- | --------------------------------------------------------- |
| `chunk_px`  | `(240, 192)`         | Store has very small or very large spatial extent         |
| `padding`   | `1`                  | Tile edge artefacts, or no padding needed                 |
| `lod_grids` | `{}` (auto-computed) | Pre-set known grids to skip the first-request computation |

---

## 18. Environment variables

Consolidated reference. Defaults match the application code; the Docker Compose overrides in `docker-compose.yml` use the same defaults.

### 18.1 Configuration philosophy — where does a new tunable belong?

This codebase holds configuration in three places. Both env vars and code constants are evaluated once at startup, so from a "when does it take effect" perspective they are equivalent — the choice of layer is a deliberate **signal** about how a value should change, not a runtime distinction.

| Layer                                                | What lives here                                                                                          | Change discipline                                                                          | Examples                                                  |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------- |
| **Env vars** (this section)                          | Operational knobs — perf, resource limits, paths, secrets. Do **not** affect wire format or shader contract. | Rotate freely at deploy; the value itself doesn't need code review.                        | `THREAD_POOL_SIZE`, `SLICE_CACHE_SIZE`, `CACHE_DAYS`, `DISK_CACHE_PATH`, `ADMIN_API_KEY` |
| **Code constants** (`constants.py`)                  | Wire / shader contracts — values that must stay in lockstep with the frontend or with the data encoding. | Change via PR so frontend and server stay in sync; the diff is the audit trail.            | `LOD.max_lods`, `LOD.min_coarsest`, `LOD.zoom_thresholds`, `CHUNK_PX`, `PADDING` (global defaults) |
| **Per-product fields** (`Product` dataclass + admin) | Data characteristics that legitimately vary across products.                                             | Set per product via `POST /admin/products`; no code change needed.                         | `chunk_px`, `padding`, `variable`, `source_path`          |

**The rule when adding a new tunable**: ask *who needs to be informed when the value changes?*

- Only the operator → **env var**.
- The frontend (or any wire-format consumer) needs a matching update → **code constant**, so the change goes through code review alongside the frontend change.
- Only one product is affected → **per-product field**, exposed via the admin API.

A wrong-layer choice has real costs: making `max_lods` an env var would let an ops engineer raise it to `6` thinking "more LODs = better detail", silently overflowing the WebGL atlas's 4096×4096 (≈64 MB VRAM) cap and triggering LRU tile thrashing — rendering still works, but UX degrades through re-upload churn that ops can't easily diagnose without frontend context. Making `THREAD_POOL_SIZE` a code constant would require a redeploy and PR for every perf-tuning experiment.

### 18.2 Server

| Variable                | Default            | Description                                                                                |
| ----------------------- | ------------------ | ------------------------------------------------------------------------------------------ |
| `TILE_TIMEZONE`         | `Australia/Sydney` | IANA timezone for date conversion. See [§10](#10-date-and-timezone-convention).            |
| `ADMIN_API_KEY`         | _(required)_       | Secret value compared against the `X-Admin-Key` header on every `/admin` request.          |
| `PRODUCTS_CONFIG_PATH`  | `products.json`    | Path to the persisted product registry. Docker overrides to `data/products.json`.          |
| `COLORMAPS_CONFIG_PATH` | `colormaps.json`   | Path to the persisted custom-colormap registry. Docker overrides to `data/colormaps.json`. |

### 18.3 Threading and cache sizing

| Variable               | Default | Description                                                                                                             |
| ---------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------- |
| `THREAD_POOL_SIZE`     | `100`   | Anyio thread-pool size. Each in-flight sync request uses one slot. See [§14](#14-concurrency-event-loop-and-threading). |
| `SLICE_CACHE_SIZE`     | `10`    | LRU size for the L2 in-memory slice cache. RAM bound: `SLICE_CACHE_SIZE × max_slice_size`.                              |
| `PROCESSED_CACHE_SIZE` | `50`    | LRU size for the L1 processed-grid cache. Sized as `SLICE_CACHE_SIZE × LOD.max_lods` with headroom.                         |
| `STORE_TTL_SECONDS`    | `600`   | Stale-while-revalidate window for the Zarr store singleton.                                                             |

### 18.4 Disk cache (L3)

| Variable                         | Default   | Description                                                                               |
| -------------------------------- | --------- | ----------------------------------------------------------------------------------------- |
| `DISK_CACHE_PATH`                | _(unset)_ | Absolute path for the disk cache. Disk caching is disabled if unset.                      |
| `DISK_CACHE_LIMIT_GB`            | `20`      | Maximum total disk usage before pressure-based eviction runs.                             |
| `DISK_EVICTION_THRESHOLD`        | `0.85`    | Fraction of limit at which pressure eviction triggers (0.0–1.0).                          |
| `CACHE_DAYS`                     | `30`      | How many recent dates per product to keep on disk; dates outside this window are evicted. |
| `PREWARM_WORKERS`                | `4`       | Thread-pool size used during the startup disk prewarm.                                    |
| `CACHE_REFRESH_INTERVAL_SECONDS` | `14400`   | Period (seconds) between background refresh cycles. Default 4 hours.                      |

See `docker-compose.yml` for the production wiring of these variables, and [`docs/security.md`](security.md) for how `ADMIN_API_KEY` interacts with nginx and the EC2 security group.
