# Technical Reference

## Table of contents

**Part I — Orientation**

1. [Overview](#1-overview)
2. [Why Zarr](#2-why-zarr)
3. [System architecture](#3-system-architecture)
4. [File layout](#4-file-layout)

**Part II — Coordinate systems & API**

5. [Tile coordinate systems and projection pipeline](#5-tile-coordinate-systems-and-projection-pipeline)
6. [URL contract and API surface](#6-url-contract-and-api-surface)

**Part III — Tile generation internals**

7. [Data-tile internals (LOD pyramid + resample + PNG encoding)](#7-data-tile-internals)
8. [Visual-tile internals (CRS guard, antimeridian, colormaps)](#8-visual-tile-internals)

**Part IV — Data conventions**

9. [Date, timezone, and coordinate normalisation](#9-date-timezone-and-coordinate-normalisation)

**Part V — Caching & runtime**

10. [Caching strategy](#10-caching-strategy)
11. [Background tasks](#11-background-tasks)
12. [Concurrency: event loop and threading](#12-concurrency-event-loop-and-threading)

**Part VI — Operations**

13. [Adding a new product](#13-adding-a-new-product)
14. [Capacity and resource planning](#14-capacity-and-resource-planning)
15. [Environment variables](#15-environment-variables)
16. [Logging](#16-logging)

---

# Part I — Orientation

## 1. Overview

The server is a FastAPI application that produces on-demand PNG tiles for IMOS ocean data products held in Zarr stores on S3.

**Scope.** This server serves **gridded data stored as Zarr** only. Every product is expected to be a Zarr store on S3 with a regular lat/lon grid (`time`, `lat`, `lon` dimensions, optionally with depth/variable axes). Non-gridded data (point observations, vessel tracks, swath/orbit data) and non-Zarr formats (NetCDF, HDF5, COG, GeoTIFF) are out of scope — the entire pipeline, from `load_slice` through the LOD algorithm to the WebGL atlas, assumes a regular gridded Zarr source. See [§2](#2-why-zarr) for _why_ Zarr, and [`docs/netcdf-vs-zarr.md`](netcdf-vs-zarr.md) for the format-comparison data behind that choice.

It exposes **two independent tile pipelines** from the same underlying data:

| Pipeline        | Output CRS               | Coordinate convention                                                                                                                       | Consumer                                             |
| --------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| `/data_tiles`   | EPSG:4326 (Plate Carrée) | Custom LOD pyramid: `z` = LOD level, `x`/`y` = chunk col/row                                                                                | WebGL shader (decodes raw values, reprojects on GPU) |
| `/visual_tiles` | EPSG:3857 (Web Mercator) | Standard XYZ slippy-map tiles (OSM/MapboxGL/Leaflet) plus a bbox endpoint and a date-range **animation** endpoint (GIF/APNG/WebP) for demos | Any map library / WMS-style consumer                 |

The same Zarr slice is the source for both pipelines; they diverge at the renderer. See [§5](#5-tile-coordinate-systems-and-projection-pipeline) for the full distinction.

There is no static product list compiled into the code. Products live in `products.json` and can be populated in two ways: pre-populated on disk **before** the server starts (typical for a fresh deployment or reproducible bootstrap), or added/removed at runtime via the **admin API**, which takes effect immediately without restart. See [§13](#13-adding-a-new-product) for both flows.

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
│  /data_tiles  ·  /visual_tiles     │  │  routers/admin/{auth,products,     │
│       products.py  (shared)        │  │              colormaps}.py         │
│  /products · /manifest · /point    │  │          X-Admin-Key               │
└──────────────────┬─────────────────┘  └────────────────────────────────────┘
                   │
                   ├───────────────────────────────────────┐
                   │                                       │
                   ▼                                       ▼
┌────────────────────────────────────┐  ┌────────────────────────────────────┐
│   rendering/data_tiles.py          │  │  rendering/visual_tiles.py         │
│   EPSG:4326 (Plate Carrée)         │  │  + colormap/resolver.py            │
│   L1 caching/processed_cache.py    │  │  + colormap/legend.py              │
│   PNG encode for WebGL shader      │  │  EPSG:4326 → EPSG:3857 + LUT       │
└──────────────────┬─────────────────┘  └──────────────────┬─────────────────┘
                   │ L1 miss                               │ every request
                   └──────────────────┬────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│       caching/slice_cache.py  +  store/registry.py                         │
│   StoreRegistry (stale-while-revalidate)    L2 Slice cache (in-memory LRU) │
│   get_store / get_available_dates           load_slice, keyed (url,d,vars) │
└────────────────────────────────────────────────────────────────────────────┘
                                      │ L2 miss
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                           L3  Disk cache                                   │
│       caching/disk.py  ·  .pkl.lz4 per date  ·  DISK_CACHE_PATH            │
└────────────────────────────────────────────────────────────────────────────┘
                                      │ L3 miss
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                              AWS  S3                                       │
│                            Zarr stores                                     │
└────────────────────────────────────────────────────────────────────────────┘
```

### Request flow

**Data tiles** (`/data_tiles/{product_id}/{date}/{z}/{x}/{y}.png`)

`load_slice` is lazy — the handler passes a callable to `render_tile`, which only invokes it when `_get_processed` misses. On a processed-cache hit, no slice I/O occurs.

```
processed warm → get_lod_grids (already set) → _get_processed (cache hit)                            → _extract_chunk → PNG encode
slice warm     → get_lod_grids (already set) → _get_processed miss → load_slice (L2 hit, <1ms)       → resample → cache → _extract_chunk → PNG encode
disk warm      → get_lod_grids (already set) → _get_processed miss → load_slice (disk read, ~30ms)   → resample → cache → _extract_chunk → PNG encode
S3 cold        → get_lod_grids (already set) → _get_processed miss → load_slice (S3 .compute(), ~2s) → resample → cache → _extract_chunk → PNG encode
```

**Visual tiles** (`/visual_tiles/{product_id}/{date}/{z}/{x}/{y}.{ext}` or `/bbox.{ext}` — `ext ∈ {png, webp}`)

No L1 cache. Every request calls `load_slice`, then `XarrayReader` reprojects to Web Mercator.

```
mem warm  → load_slice (L2 hit, <1ms)        → _to_scalar_parts (antimeridian split if needed) → XarrayReader.tile/part → colormap + PNG encode
disk warm → load_slice (disk read, ~30ms)    → _to_scalar_parts → XarrayReader.tile/part → colormap + PNG encode
S3 cold   → load_slice (S3 .compute(), ~2s)  → _to_scalar_parts → XarrayReader.tile/part → colormap + PNG encode
```

---

## 4. File layout

```
imos-tiler/
  src/app/
    main.py                      ← mounts all routers, CORS middleware, lifespan startup
    constants.py                 ← LOD/LODConfig + TILE/TileConfig (server-shader contract), CACHE_VERSION, COORD_NAMES
    config/
      paths.py                   ← PRODUCTS_CONFIG_PATH, COLORMAPS_CONFIG_PATH, DISK_CACHE_PATH
    log_config.py                ← logging setup (JSON in Docker, coloured text locally)
    routers/
      data_tiles.py              ← /data_tiles — raw value-encoded RGBA tiles for WebGL
      visual_tiles.py            ← /visual_tiles — colourised Web Mercator XYZ tiles + bbox + colormap listing/legend
      products.py                ← shared: /products, /manifest, /{id}/{date}/point — included by both tile routers
      shared.py                  ← shared router helpers (PRODUCT_EX/DATE_EX examples, get_product_or_404, load_slice_or_404)
      admin/                     ← /admin — product, colormap, and cache-state endpoints (key-protected, package)
        __init__.py              ← assembles admin_router and applies require_admin_key
        auth.py                  ← X-Admin-Key dependency
        products.py              ← POST/DELETE /admin/products
        colormaps.py             ← POST/DELETE /admin/colormaps
        cache.py                 ← GET /admin/cache (state snapshot) + DELETE /admin/cache/memory (clear L1+L2) + DELETE /admin/cache/disk (clear L3)
    services/
      caching/
        lifecycle.py             ← cross-layer orchestration: prewarm, refresh, stale eviction, evict_product_cache fan-out
        slice_cache.py           ← L2 LRU + load_slice + evict_slice_cache_for_product
        processed_cache.py       ← L1 processed-grid cache + memoizer + per-product eviction
        disk.py                  ← L3 disk IO + per-file/dir eviction: path, read/write, pressure eviction, clear, stats
      colormap/
        registry.py              ← colormaps.json read/write + in-memory colormap registry + ColormapMode + invalidation hooks
        resolver.py              ← resolve_colormap() — custom→rio-tiler→matplotlib fallback chain
        legend.py                ← render_legend() — color bar + tick labels
      product/
        product.py               ← Product dataclass + LOD algorithm + get_lod_grids lazy-init
        registry.py              ← PRODUCTS dict + load/register/remove + get_product / iter_products facades
        manifest.py              ← render_manifest() — product introspection (bounds + per-variable ranges + LOD meta)
      rendering/
        kernels.py               ← numba JIT bilinear + normalize kernels + xr.interp fallback + warmup_resample
        data_tiles.py            ← render_tile() — chunk extract + RGBA pack + PNG encode (data tiles)
        visual_tiles.py          ← render_tile / render_bbox / render_bbox_animation — Web Mercator (visual tiles)
      store/
        registry.py              ← Zarr store singleton (stale-while-revalidate) + per-URL date index + get_available_dates
        spatial.py               ← bbox_to_wgs84 + native_resolution_in_bbox + default_bbox_from_store
    utils/
      dates.py                   ← LOCAL_TZ + ts_to_local_date + three_months_ago
      geo.py                     ← dataset_bounds + json_safe_float
      colors.py                  ← hex parsing + ramp/categorical LUT builders
      memoizer.py                ← shared dedup+cache helper used by load_slice, processed cache, visual-tile dedup
      image.py                   ← encode_rgba(arr, fmt) + empty_tile(fmt) + media_type(fmt) — PNG/WebP encoders shared by both renderers
  docker/
    Dockerfile
    docker-entrypoint.sh
    nginx.conf
  tests/
  data/
    products.json                ← persisted product registrations (runtime, gitignored; mounted volume in Docker)
    colormaps.json               ← persisted custom colormap registrations (runtime, gitignored; mounted volume in Docker)
  docs/
    technical.md                 ← this file
    cache_analysis.md            ← cache design decision record (Redis vs EFS vs EBS vs ephemeral)
    dataset.md                   ← per-store variable / dimension / chunking reference
    netcdf-vs-zarr.md            ← format comparison, IMOS product file analysis, performance data
    security.md                  ← admin endpoint protection (key + nginx + EC2 security group)
```

All three runtime paths are hardcoded constants in `src/app/constants.py` — not env vars:

| Constant                | Value                 | Notes                                                                                     |
| ----------------------- | --------------------- | ----------------------------------------------------------------------------------------- |
| `PRODUCTS_CONFIG_PATH`  | `data/products.json`  | Auto-created by `docker-entrypoint.sh` if absent; `./data` bind-mounted in Docker.        |
| `COLORMAPS_CONFIG_PATH` | `data/colormaps.json` | Same as above.                                                                            |
| `DISK_CACHE_PATH`       | `slice_cache`         | Relative to working directory (`/app` in Docker); `./slice_cache` bind-mounted in Docker. |

---

# Part II — Coordinate systems & API

## 5. Tile coordinate systems and projection pipeline

The server produces tiles in **two different coordinate reference systems** depending on the endpoint. The two pipelines share the URL shape `/{product_id}/{date}/{z}/{x}/{y}.png` but interpret `z`, `x`, `y` in entirely different coordinate systems. Mixing them up is the most common cause of "why is my tile blank / 404 / off-by-one" bugs.

### 5.1 Which API should I use?

- Building a normal map with Mapbox GL, MapLibre, Leaflet, OpenLayers, etc., and you just need pretty raster tiles overlaid on a base map → **`/visual_tiles`**.
- Building a custom WebGL visualisation where the client needs the raw scientific values (dynamic colour ramps, client-side analysis, particle animation on UV data) → **`/data_tiles`**.

### 5.2 Two pipelines, two CRSs

|                                 | `/data_tiles`                                                                       | `/visual_tiles`                                                                |
| ------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| **Output CRS**                  | EPSG:4326 (Plate Carrée)                                                            | EPSG:3857 (Web Mercator)                                                       |
| **`z` meaning**                 | LOD index (`1` = coarsest, `N` = finest)                                            | Zoom level (`0` = whole world; each step doubles tiles per axis)               |
| **`(x, y)` reference frame**    | Product's own extent (NW corner = `0, 0`)                                           | Whole world (Web Mercator origin = `0, 0`)                                     |
| **`(x, y)` range at level `z`** | `0` to `lod_grids[z] − 1`                                                           | `0` to `2^z − 1`                                                               |
| **Out-of-range `(z, x, y)`**    | HTTP 404                                                                            | HTTP 400 (invalid coords); transparent 256×256 PNG (in-range but outside data) |
| **Pixel content**               | Raw value packed into RGBA bytes (24-bit normalised uint or two 8-bit U/V channels) | Colourised RGBA image after applying a colormap LUT                            |
| **Reprojection happens…**       | In the **WebGL fragment shader** on the client, on the GPU                          | On the **server**, by `rio-tiler`'s `XarrayReader.tile(...)`                   |
| **Multi-variable support**      | Yes (UV products such as `ocean_current`)                                           | No (single-variable products only)                                             |
| **Per-tile decode manifest**    | Required (`/{product_id}/{date}/manifest.json`)                                     | Not applicable                                                                 |
| **Extra non-tile endpoint**     | —                                                                                   | `/bbox` (arbitrary region, EPSG:4326 or EPSG:3857)                             |

The data-tiles `z` axis indexes a **custom LOD pyramid** anchored to the product's own extent — see [§7](#7-data-tile-internals) for the algorithm that derives the pyramid from each Zarr store's dimensions.

#### `data_tiles` — `z`/`x`/`y` semantics

`z` selects a **resolution level**, not a map-zoom level. The valid values are the keys of `product.lod_grids` (typically `1`–`4`); `z = 1` is the coarsest, `z = N` is native data resolution. The LOD grids are derived at server startup from each Zarr store's actual lat/lon dimensions and the fixed chunk size (`CHUNK_PX = (240, 192)`). The client maps map-zoom to LOD via the universal `LOD.zoom_thresholds` returned in each level's `zoomThreshold` field.

`x` and `y` are chunk column/row within the LOD grid: `x = 0` is the westernmost column, `y = 0` is the northernmost row. Valid range at LOD `z` is `0 ≤ x < grid_cols` and `0 ≤ y < grid_rows`, where `(grid_cols, grid_rows) = product.lod_grids[z]`. Requesting outside this range returns **HTTP 404**. Clients are expected to fetch the manifest first so they know each LOD's grid dimensions.

#### `visual_tiles` — `z`/`x`/`y` semantics

`z`, `x`, `y` are **standard Web Mercator slippy-map tile coordinates** — identical to OpenStreetMap, MapboxGL, MapLibre, Leaflet, and OpenLayers. At zoom `z`, the world is divided into a `2^z × 2^z` grid; `x = 0` is the leftmost column, `y = 0` is the topmost row. Valid range is `0 ≤ x, y ≤ 2^z − 1`. Out-of-range coordinates (e.g. `x = 2^z`) return **HTTP 400** — the URL is malformed. In-range tiles that fall **outside the product's data extent** return a **transparent 256×256 PNG** (not an error), so clients can request a full world grid without first checking the data bounds.

### 5.3 Data tiles — generated in EPSG:4326 (Plate Carrée)

Source Zarr data lives on a regular lat/lon grid. Data tiles preserve that grid exactly: longitude maps linearly to pixel X, latitude maps linearly to pixel Y. This is Plate Carrée — the visual representation of EPSG:4326 / WGS84 geographic coordinates.

The projection is implemented implicitly in `resample_variables_to_grid` (`services/rendering/kernels.py`):

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

### 5.4 Visual tiles — generated in EPSG:3857 (Web Mercator)

`services/rendering/visual_tiles.py` calls `XarrayReader.tile(x, y, z, reproject_method="bilinear")`. The reader internally:

1. Reads the source slice (already tagged `EPSG:4326` via `da.rio.write_crs("EPSG:4326")`).
2. Computes the Web Mercator footprint of the target tile from `(x, y, z)`.
3. Reprojects the relevant 4326 region into a 256×256 Mercator-grid array using bilinear interpolation.
4. Returns the array, which the renderer then rescales (per `rescale` or auto-derived min/max), maps through the colormap LUT, and PNG-encodes.

Because the output PNG is already in Web Mercator, visual tiles work directly with any map library that consumes XYZ Web Mercator tiles — MapboxGL `raster` sources, Leaflet, OpenLayers, Mapbox `{bbox-epsg-3857}` raster placeholders, etc. **No client-side reprojection is required.**

The `/bbox.{ext}` endpoint follows the same pipeline using `reader.part(...)`; it accepts the bbox in either EPSG:4326 or EPSG:3857 (controlled by `?crs=`) and produces a Web Mercator image in the requested format.

### 5.5 Frontend integration

The frontend in production renders a Web Mercator base map (typical of every map library: Mapbox, MapLibre, Leaflet, OpenLayers, Google Maps).

- **Visual tiles** plug straight into a `raster` source — no shader and no per-frame math.
- **Data tiles** are sampled by a custom **WebGL fragment shader**. The shader does the work the server intentionally skipped: for each fragment's Mercator position it computes the inverse Mercator to recover `(lon, lat)`, then samples the Plate-Carrée atlas via a linear lat/lon lookup — matching the server's `np.linspace` mapping. Value decoding (uint24 → float via the manifest's `valueRange`) and colour-ramp lookup happen in the same pass.

### 5.6 The manifest is the contract between server and shader

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

`z`/`x`/`y` mean different things in each tile API — see [§5](#5-tile-coordinate-systems-and-projection-pipeline).

### 6.1 Shared endpoints (mounted under both `/data_tiles` and `/visual_tiles`)

`routers/products.py` is included by both tile routers, so these paths exist under both prefixes:

```
GET /{prefix}/products                                          → list all registered products
GET /{prefix}/manifest?from=YYYY-MM-DD&to=YYYY-MM-DD             → available dates for all products
GET /{prefix}/{product_id}/{date}/point?lat=&lon=                → variable value at one date
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

**`/point` cache headers — immutable.** The single-date `/{product_id}/{date}/point` form uses `IMMUTABLE_CACHE_HEADERS` (`max-age=31536000, immutable`) because the date is in the **path** — once that date's data exists, the URL → bytes mapping is pinned forever.

### 6.2 Data tiles (`/data_tiles`)

```
GET /data_tiles/{product_id}/{date}/{z}/{x}/{y}.png       → raw RGBA PNG tile
GET /data_tiles/{product_id}/{date}/manifest.json         → bounds + value ranges + LOD grid config
```

`z` = LOD level, `x` = chunk column (`0` = westernmost), `y` = chunk row (`0` = northernmost).

### 6.3 Visual tiles (`/visual_tiles`)

Colourised PNG tiles in standard Web Mercator (XYZ). Single-variable products only.

```
GET /visual_tiles/colormaps                                            → all supported colormap names
GET /visual_tiles/colormaps/{name}/legend                              → color legend PNG for a colormap
GET /visual_tiles/{product_id}/{date}/{z}/{x}/{y}.{ext}                  → colourised Web Mercator image (.png or .webp)
GET /visual_tiles/{product_id}/{date}/bbox.{ext}?bbox=minx,miny,maxx,maxy → colourised image for arbitrary bbox (.png or .webp)
GET /visual_tiles/{product_id}/{from_date}/{to_date}/animation.{ext}    → animated bbox across a date range (.gif, .apng, .webp)
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

#### 6.3.1 Animation endpoint

Renders the same bbox across every available date in `[from_date, to_date]` and assembles them into a single animated image. Intended for demos and quick visualisations — **not** a hot-path endpoint.

```
GET /visual_tiles/{product_id}/{from_date}/{to_date}/animation.{ext}
```

`ext` ∈ `gif`, `apng`, `webp`. Single-variable products only. `from_date` must be ≤ `to_date`.

**Query parameters:**

| Query param | Default                               | Description                                                                                                                                                                                                              |
| ----------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bbox`      | dataset's native extent               | `minx,miny,maxx,maxy` in the CRS specified by `crs`. When omitted, the dataset's lat/lon bounds are used (clamped to ±180° lon for antimeridian-straddling grids; pass `bbox` explicitly to render the slice past 180°). |
| `width`     | _(see "Resolution defaulting" below)_ | Output frame width in pixels (1–2048).                                                                                                                                                                                   |
| `height`    | _(see "Resolution defaulting" below)_ | Output frame height in pixels (1–2048).                                                                                                                                                                                  |
| `colormap`  | `viridis`                             | Colormap name. Categorical colormaps require `rescale`; animated WebP is rejected for categorical (use `.apng` or `.gif`).                                                                                               |
| `rescale`   | union of all frames                   | `min,max`. The default spans the union of every requested date so the colour ramp is stable frame-to-frame; auto-ranging per frame would flicker.                                                                        |
| `crs`       | `EPSG:4326`                           | CRS of the explicit `bbox`. The default bbox is always returned in EPSG:4326 regardless of `crs`.                                                                                                                        |
| `duration`  | `200`                                 | Milliseconds per frame (10–5000).                                                                                                                                                                                        |

**Resolution defaulting** — three branches in `_resolve_resolution` (`routers/visual_tiles.py`):

| Input                 | Output                                                                                                                                                                                                                                                |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Both `width`+`height` | Used as given.                                                                                                                                                                                                                                        |
| Both omitted          | Frame size matches the dataset's native cell count inside the bbox, i.e. `ceil(bbox_span / native_spacing)` per axis, **clamped to 2048**. Native spacing is read from the first two lat/lon coordinates — all current products are on regular grids. |
| Only one provided     | The other is derived from the bbox aspect ratio in the bbox's own CRS (`(maxx-minx)/(maxy-miny)`) so the output is not stretched relative to the requested view. Clamped to 1–2048.                                                                   |

**Frame cap** — 30 frames per request, hard-coded in `_MAX_ANIMATION_FRAMES`. Requests beyond that are rejected with 400 so a wide date range can't produce a multi-hundred-megabyte response, and so worst-case transient RAM and cold-S3 latency for a single animation stay bounded.

**Caching design** — this endpoint deliberately differs from the other tile endpoints:

| Layer                      | Behaviour                                                                                                                                                                                                                                                                            |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| In-memory slice cache (L2) | **Bypassed.** Animations call `load_slice_uncached` (`services/caching/slice_cache.py`) which never touches the LRU. A rare 30-frame request can therefore not evict hot slices serving the static `/visual_tiles` and `/data_tiles` endpoints.                                                   |
| Disk cache (L3)            | **Read-through.** If the prewarmed disk slice exists it is reused; otherwise the slice is fetched directly from the Zarr store. Animations never **write** to L3 — they may request dates outside the prewarmed window and we don't want to pollute L3 or trigger an eviction cycle. |
| HTTP cache headers         | **None.** No `Cache-Control` set. CloudFront/CDN configurations should treat this path as no-cache; otherwise rare requests would still incur full origin cost while occupying CDN storage.                                                                                          |

**Frame loading** — the handler is `async def`. Per-frame `load_slice_uncached` calls are dispatched in parallel via `asyncio.gather(*(anyio.to_thread.run_sync(..., limiter=_ANIMATION_LIMITER) for ...))`, so a cold N-frame request blocks on roughly the slowest single-frame S3 read rather than the serial sum. Frame order is preserved because `gather` returns results in input order. The work runs on the shared anyio pool but under `_ANIMATION_LIMITER` (`ANIMATION_WORKERS`, default 10), a budget independent of the default tile-handler budget — a 30-frame fan-out can't starve tile-handler slots. See [§12.6](#126-one-pool-three-named-budgets).

The store-touching prelude (`get_available_dates`, `_default_bbox_from_store`, native-resolution lookup) is also offloaded via `anyio.to_thread.run_sync` so the event loop never blocks on `get_store` if a cold open or TTL-driven re-open is in progress.

The end-user experience: cold requests are still slow (every missing date is an S3 round-trip), repeat requests don't get faster, but no other endpoint is affected.

### 6.4 Admin API (`/admin`)

All endpoints require the `X-Admin-Key` header. The expected value is read from the `ADMIN_API_KEY` environment variable. See [`docs/security.md`](security.md) for the full key/nginx/security-group layering.

```
POST   /admin/products              → register a new product
DELETE /admin/products/{product_id} → remove a product

POST   /admin/colormaps             → register a custom colormap
DELETE /admin/colormaps/{name}      → remove a custom colormap

GET    /admin/cache                 → cache-state snapshot (see §10.6)
DELETE /admin/cache/memory          → clear all in-memory caches (L1 + L2); disk untouched (see §10.7)
DELETE /admin/cache/disk            → delete every slice file from the L3 disk cache; memory untouched (see §10.8)
```

---

# Part III — Tile generation internals

## 7. Data-tile internals

Everything specific to the `/data_tiles` pipeline that the [coordinate-systems section](#5-tile-coordinate-systems-and-projection-pipeline) did not cover: how the LOD pyramid is derived from each Zarr store, and how each tile is encoded as RGBA bytes the WebGL shader can decode.

This applies to data tiles only — visual tiles use Web Mercator zoom levels and ordinary colourised PNGs (see [§8](#8-visual-tile-internals)).

### 7.1 LOD constants (`constants.py`)

The three LOD knobs are bundled into a single frozen-dataclass instance, `LOD = LODConfig()`. They are **not** environment variables — these values are baked into the WebGL shader on the frontend, so changing one without redeploying the frontend silently corrupts the rendering.

- `LOD.max_lods = 4` — cap on LOD levels per product. The frontend packs all LODs into a single WebGL texture atlas hard-capped at 4096×4096 px (≈64 MB VRAM per atlas) regardless of `gl.MAX_TEXTURE_SIZE`. Going above 4 doesn't break rendering — the atlas falls back to LRU eviction — but causes visible tile re-upload churn as the user pans or zooms. `4` is tuned to fit comfortably under the cap for current product sizes.
- `LOD.min_coarsest = (2, 2)` — minimum (cols, rows) for the coarsest LOD level; levels below this are dropped. If all levels are filtered out (data smaller than one chunk), falls back to the native finest grid so there is always at least one LOD.
- `LOD.zoom_thresholds: dict[LODIndex, ZoomLevel]` — maps each LOD index to the minimum map-zoom level at which the shader activates it (e.g. `{2: 4, 3: 5, 4: 6}` means LOD 2 is used at map-zoom ≥ 4, LOD 3 at ≥ 5, etc.). The shader reads these values from the manifest to decide which LOD to request at each map-zoom. If the server and frontend disagree on these thresholds, the shader requests nonexistent LOD levels or fetches the wrong resolution — the atlas mapping breaks silently and data tiles are useless regardless of whether they are individually served correctly. Treat any change to `zoom_thresholds` with the same caution as `max_lods` and `min_coarsest`: it requires a coordinated frontend redeploy.

### 7.2 LOD algorithm (`Product._compute_lod_grids` in `services/product/product.py`)

Derives LOD grids from actual data dimensions and chunk size. Accepts `max_lods` and `min_coarsest` as parameters (defaulting to `LOD.max_lods` and `LOD.min_coarsest`).

1. Finest level: `ceil(data_width / chunk_w) × ceil(data_height / chunk_h)`.
2. Depth: `floor(log2(max(finest_cols, finest_rows)))` — number of halvings before both axes reach 1 (uses `max` so elongated grids go as deep as the wider axis allows).
3. Each level `k`: `(ceil(finest_cols / 2^k), ceil(finest_rows / 2^k))` — `ceil` preserves coverage at intermediate scales (e.g. `finest=5` → `3, 2` not `2, 1`).
4. Drop levels whose cols or rows fall below `min_coarsest`. If nothing remains (data fits within a single chunk), fall back to `(finest_cols, finest_rows)` directly.
5. Take the finest `max_lods` levels; assign LOD indices starting at 1 (coarsest).

Example: `Product._compute_lod_grids(3000, 1500, (256, 256))` → `{1: (3, 2), 2: (6, 3), 3: (12, 6)}`.

Small-dataset example (radar SA Gulfs, 102×74, chunk 240×192): finest=(1,1), filtered to nothing, fallback → `{1: (1, 1)}`.

### 7.3 Lazy population (`services/product/product.py` — `get_lod_grids`)

Products start with `lod_grids={}`. On the first request:

1. `get_lod_grids(product)` checks `product.lod_grids` — empty, so proceeds (double-checked locking).
2. Opens the Zarr store (singleton — reused across all calls to the same URL).
3. Reads lat/lon dimension sizes from store metadata (`.zmetadata`, no data fetch).
4. Calls `product.apply_computed_lod_grids(data_width, data_height)`, which runs `_compute_lod_grids` and populates the result via `self.lod_grids.update()`. Although `Product` is a frozen dataclass, `lod_grids` is a mutable dict — `update()` mutates the dict in place without reassigning the attribute, so no frozen-bypass is needed.
5. All subsequent calls return immediately from the `if product.lod_grids` guard.

### 7.4 Resample and normalize (numba JIT)

The hot path for every cold-L1 data tile is two CPU-bound steps in `services/rendering/kernels.py` (called from `services/rendering/data_tiles.py`):

1. **Bilinear resample** (`resample_variables_to_grid`) — interpolates the source Zarr slice onto the LOD's `total_w × total_h` grid. Output pixel positions match `np.linspace(0, src-1, total)` on both axes — the same mapping the WebGL shader assumes (see [§5.6](#56-the-manifest-is-the-contract-between-server-and-shader)).
2. **Normalize + ocean mask** (`_numba_normalize_uint32` / `_numba_normalize_uint8`, dispatched via `normalize()`) — clips each variable into its byte-range output and produces the per-pixel valid mask in a single pass.

Both steps are implemented as `@njit`-compiled numba kernels. Switching from `xr.interp` + numpy normalize to the numba kernels was a ~5× speedup on Intel EC2 — see the benchmark below.

#### Why numba

Bilinear interpolation is trivial math (~7 FLOPS/pixel) over millions of pixels. The work is bound by single-thread SIMD throughput and memory bandwidth, not by anything xarray/scipy provide. `xr.interp(method="linear")` goes through scipy's `interpn` wrapper, which has significant Python/array-allocation overhead at the LOD-4 grid (≈8.6 M pixels). On Apple Silicon the Accelerate framework hides this cost; on Intel/x86 it does not.

We benchmarked several alternatives on EC2 `t3.large` against a real cached SSTA slice (`scripts/benchmark_resample.py`):

| Method                          | LOD 4 (4080×2112) | Max diff vs xr.interp | NaN mask match |
| ------------------------------- | ----------------: | --------------------: | -------------: |
| `xr.interp` (baseline)          |            419 ms |                 exact |           100% |
| `scipy.ndimage.zoom`            |            718 ms |                 0.001 |            96% |
| `scipy.RegularGridInterpolator` |            748 ms |        ~float epsilon |           100% |
| `scipy.ndimage.map_coordinates` |            974 ms |                 0.001 |            96% |
| `PIL.Image.resize` BILINEAR     |            175 ms |              0.099 °C |          99.9% |
| **numba parallel bilinear**     |         **76 ms** |             **0.001** |       **100%** |

The numba kernel wins by ~5.5× while preserving full NaN-mask fidelity. PIL is fast but uses pixel-center sampling coordinates that produce visible ~1 °C systematic errors at coarse LODs — wrong for visualisation. The scipy alternatives all scale worse than xarray at LOD 4 because building the explicit 2-D point cloud is slower than xarray's vectorised dispatch.

End-to-end production impact on EC2 `t3.large` (cold L1, LOD 4, disk-cached date):

| Step      | Before (xr.interp + numpy) | After (numba) |  Speedup |
| --------- | -------------------------: | ------------: | -------: |
| disk_read |                     167 ms |        159 ms |        — |
| resample  |                     419 ms |     **82 ms** |     5.1× |
| normalize |                     199 ms |     **48 ms** |     4.1× |
| encode    |                      18 ms |         30 ms |        — |
| **total** |                 **843 ms** |    **325 ms** | **2.6×** |

#### The `fastmath` flag gotcha

Both kernels use `@njit(fastmath=...)` for SIMD vectorisation, **but with different flag sets** — and this is load-bearing.

- **`_numba_bilinear`** uses `fastmath=True` (all flags, including `nnan`). Safe here because the kernel does no explicit `np.isnan` check on its hot path — NaN propagates through hardware FP arithmetic (`a * (1-dx) + b * dx` returns NaN if any operand is NaN, regardless of the compile-time `nnan` flag). Verified against `xr.interp` (100 % NaN-mask match in the benchmark above).
- **`_numba_normalize_*`** uses **selective fastmath** that excludes `nnan`: `fastmath={"nsz", "arcp", "contract", "afn", "reassoc"}`. The kernel calls `np.isnan(v)` explicitly to fold the valid-mask scan into the normalize pass. **Under `fastmath=True` with `nnan` set, the LLVM optimiser collapses `np.isnan` to always-False** — silently breaking land detection so land pixels render as opaque black. (This bug shipped briefly during development. Don't repeat it.)

The explicit `if np.isnan(...)` block in `_numba_bilinear` is dead code under the current `fastmath=True` setting but is kept for readability and as a guard if `fastmath` is ever lowered.

#### One-pass normalize + mask

A natural-looking alternative is to compute the valid mask in a separate kernel (so it can call `np.isnan` safely) and pass it into a `fastmath=True` normalize kernel. We tried this; it was ~50 % slower than the one-pass version. Each kernel reads the full 8.6 M-pixel float32 array — two kernels = two reads. The current design keeps both computations in the same loop body, reading the array once.

#### Startup warmup

`warmup_resample()` is called from the lifespan in `main.py` before the server begins serving. It invokes each kernel on a 16×16 synthetic dataset to trigger numba's JIT compile (1–3 s) and prime the on-disk `cache=True` module. Without this, the _first_ user request after every process restart would pay the full compile cost. The persisted cache means subsequent restarts skip compilation entirely.

If numba ever fails to import, the module falls back to `xr.interp` for resample and the original `_normalize` (with separate `np.isnan` ocean-mask pass) for normalize. A warning is logged at module-load time so the regression is visible.

### 7.5 PNG encoding contract

Data tiles are RGBA PNGs (`optimize=False`). The byte layout is fixed and consumed by a WebGL shader:

- **24-bit scalar** (GSLA, SSTA, WDIR, etc.): R=high byte, G=mid byte, B=low byte of a normalised uint24; A=ocean mask (255=ocean, 0=land). Land pixels have RGB zeroed (premultiplied form).
- **UV vector** (e.g. ocean current): R=U normalised to 8-bit, G=V normalised to 8-bit, B=ocean mask × 255, A=255.

Normalisation ranges (`valueRange`, `uRange`/`vRange`) are computed from the full pre-resampled dataset and returned in `manifest.json`. All tiles for a date share the same ranges.

Visual tiles do **not** use this contract — they return ordinary colourised PNGs after applying a colormap LUT.

---

## 8. Visual-tile internals

Everything specific to the `/visual_tiles` pipeline: how the renderer guards against unexpected CRSs, how datasets that straddle the antimeridian are handled, and how colormaps are looked up and rendered.

`services/rendering/visual_tiles.py` uses rio-tiler's `XarrayReader`, which requires data in **EPSG:4326** (geographic lat/lon degrees) with bounds strictly within `(−180, −90, 180, 90)`.

### 8.1 CRS guard

`_to_scalar_parts` validates coordinate ranges before passing data to `XarrayReader`:

- `lat ∈ [−90, 90]`
- `lon ∈ [−180, 360]` (allows 0–360 convention before normalisation)

A dataset in a projected CRS (e.g. UTM, GDA94/MGA) would have coordinate values in the millions and is rejected immediately with a descriptive `ValueError`. This prevents silent mis-rendering — the hardcoded `write_crs("EPSG:4326")` call would otherwise label projected coordinates as geographic without error.

### 8.2 Antimeridian handling

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

### 8.3 Colormap system

Visual tiles support any colormap name that resolves through the following lookup chain (first match wins):

1. **Custom registry** (`colormaps.json`) — names registered at runtime via the admin API.
2. **rio-tiler built-ins** — e.g. `viridis`, `plasma`, `inferno`.
3. **matplotlib** — any name from `matplotlib.colormaps`, including diverging maps like `RdBu_r`, `coolwarm`.

An unrecognised name returns `400 Bad Request`.

**Listing supported colormaps.** `GET /visual_tiles/colormaps` returns all supported names grouped by source, with higher-priority sources excluding duplicate names from lower ones:

```json
{
  "custom": [{ "name": "imos_sst", "mode": "ramp" }],
  "rio_tiler": ["accent", "algae", "viridis", "..."],
  "matplotlib": ["Blues", "RdBu_r", "coolwarm", "..."]
}
```

**Custom colormaps.** Registered via `POST /admin/colormaps` and persisted in `colormaps.json`. Loaded on startup by `load_colormaps()` in `services/colormap/registry.py` and take effect immediately without a server restart. All colormap state lives in `colormap/registry.py` — no other module holds it directly. Runtime lookup (custom → rio-tiler → matplotlib fallback) is implemented in a separate module, `services/colormap/resolver.py`, which subscribes to `colormap/registry`'s invalidation hooks to clear its LRU caches whenever the registry changes.

All colormaps are stored internally as **256-entry RGBA LUTs** (one tuple per normalised byte value, where 0 = data minimum and 255 = data maximum after `rescale`). The `POST /admin/colormaps` payload normalises the input to this format at registration time.

**Colormap modes.** The `mode` field on `POST /admin/colormaps` controls how the input stops are expanded to the 256-entry LUT:

| Mode             | `entries` format              | Behaviour                                                                |
| ---------------- | ----------------------------- | ------------------------------------------------------------------------ |
| `ramp` (default) | 2–256 colour stops            | Evenly-spaced stops linearly interpolated to 256 entries                 |
| `categorical`    | dict `{"<int>": colour, ...}` | Each integer value maps to one LUT slot; all other slots are transparent |

Each colour stop (in both modes) may be a CSS hex string (`#rgb`, `#rrggbb`, `#rrggbbaa`) or a `[r, g, b, a]` list. Hex strings without alpha default to fully opaque (a=255).

_Ramp example_ — 5 stops interpolated across the full LUT:

```json
{
  "name": "ocean_depth",
  "mode": "ramp",
  "entries": ["#000080", "#00ffff", "#ffffff", "#ff8c00", "#8b0000"]
}
```

_Categorical example_ — discrete class values 1–4:

```json
{
  "name": "land_cover",
  "mode": "categorical",
  "entries": { "1": "#ffff00", "2": "#0000ff", "3": "#ff0000", "4": "#000000" }
}
```

For categorical colormaps, `rescale=min,max` is **required** at render time and must match the range of the integer keys (e.g. `?rescale=1,4`). Omitting `rescale` with a categorical colormap returns `400 Bad Request`. This is enforced because the renderer auto-rescales to the per-tile data range when `rescale` is absent, which would corrupt the LUT slot mapping.

The data range for a categorical colormap is inferred from the key range (`min(keys)` → `max(keys)`) at registration time and used to place each value in the LUT. Values not covered by any key render as fully transparent.

**Categorical colormaps are dataset-specific.** A categorical colormap is tightly coupled to a specific variable's integer encoding — equivalent to the CF convention `flag_values` + `flag_colors` pair that ncWMS reads from dataset attributes. The `entries` keys must exactly match the discrete integer values that appear in the dataset.

The server does **not** validate this coupling. The colormap and product are registered independently, so applying a categorical colormap to a dataset with a different value encoding will render without error but produce silently wrong colours. For example, a colormap registered with keys `{1, 2, 3, 4}` applied to a dataset whose actual values are `{0, 1, 2, 3}` will shift every colour by one slot.

Practical rules:

- One categorical colormap = one dataset variable encoding. Do not reuse a categorical colormap across products unless they share the exact same integer values.
- Name categorical colormaps after the dataset or variable they describe (e.g. `land_cover_classes`, `ocean_current_flag`) to make the coupling explicit.
- Ramp colormaps are dataset-agnostic; categorical colormaps are not.

**Cache behaviour.** `resolve_colormap()` in `services/colormap/resolver.py` is `@lru_cache`-d (max 64 entries). `colormap/legend.render_legend()` caches the final PNG bytes (max 256 entries) and converts the colormap dict to a numpy array inline. The caches are cleared automatically whenever a colormap is added or deleted via the admin API — `colormap/registry._reload()` invokes the registered invalidation hooks, which include `resolve_colormap.cache_clear()` and `render_legend.cache_clear()`.

### 8.4 Output format (PNG vs WebP)

The tile and bbox endpoints take the output format as a `.{ext}` path-param suffix:

```
GET /visual_tiles/{id}/{date}/{z}/{x}/{y}.png         → image/png
GET /visual_tiles/{id}/{date}/{z}/{x}/{y}.webp        → image/webp
GET /visual_tiles/{id}/{date}/bbox.png?bbox=...       → image/png
GET /visual_tiles/{id}/{date}/bbox.webp?bbox=...      → image/webp
```

Why both formats:

- **PNG** is lossless; the only safe choice for categorical colormaps (hard colour boundaries) and the default everywhere else for backward compatibility.
- **WebP (lossy, q=85)** is typically 40–70% smaller than PNG for smooth colour ramps — the common visual-tile case. Encode time is comparable to PNG (lossy WebP is fast; lossless WebP is the slow one and is not exposed here). The visual quality difference is imperceptible for ocean-render output.

**Categorical colormaps reject `.webp`** with HTTP 400. Lossy compression introduces ringing/blocking around the discrete colour transitions that define a categorical map, which would silently corrupt the rendered classes. The router uses `is_categorical(colormap_name)` to gate this in `_reject_webp_for_categorical`.

**Format choice is per-URL, not per-request.** Each `.{ext}` is a distinct path, so CDNs/browsers cache PNG and WebP independently with no `Vary` header gymnastics. Implementation lives in `utils/image.py` (`encode_rgba`, `empty_tile`, `media_type`) so adding another format (e.g. JXL) is one branch.

The legend endpoint stays PNG-only — it's cached aggressively via `@lru_cache(maxsize=256)` and served with 1-year `Cache-Control: immutable` ([`http_caching.md`](http_caching.md)), so the per-byte win from WebP is not worth the API complexity for an image whose bytes ship from cache forever after the first encode.

The full format-evaluation history (including why **data tiles** cannot use WebP — lossy corrupts uint24 data, lossless is 115× slower than PNG) is in [`docs/png-vs-webp-vs-bin.md`](png-vs-webp-vs-bin.md).

---

# Part IV — Data conventions

## 9. Date, timezone, and coordinate normalisation

Conventions applied at store-open and date-parsing time so that all downstream code sees a uniform shape regardless of what the source Zarr store happens to use natively. **The timezone rule is the most critical invariant in this system** — getting it wrong causes silent 404s or data served for the wrong day.

### 9.1 The timezone rule

| Layer                        | Representation                                                                        |
| ---------------------------- | ------------------------------------------------------------------------------------- |
| Zarr store `time` coordinate | UTC — numpy `datetime64[ns]` is always UTC by convention                              |
| API request/response dates   | Local time in `TILE_TIMEZONE` (default `Australia/Sydney`, AEST UTC+10 / AEDT UTC+11) |

`TILE_TIMEZONE` is an IANA timezone name read at startup. To deploy this server for a different region, set it in `.env` or `docker-compose.yml` before starting — no code changes needed. All date conversion (manifest output, tile request matching, error messages) uses the configured timezone automatically.

All satellite passes over Australia occur during Australian daytime. Their UTC timestamps typically fall on the **previous UTC day** (e.g. a pass at `2022-06-01 01:20 AEST` is `2022-05-31 15:20 UTC`). Comparing UTC dates to local request dates directly would return a 404 for every such record.

### 9.2 How the server handles dates

`LOCAL_TZ` is read once at startup from the `TILE_TIMEZONE` environment variable in `utils/dates.py`:

```python
LOCAL_TZ = ZoneInfo(os.environ.get("TILE_TIMEZONE", "Australia/Sydney"))

def ts_to_local_date(ts) -> str:
    return str(pd.Timestamp(ts).tz_localize("UTC").tz_convert(LOCAL_TZ).strftime("%Y-%m-%d"))
```

Every point where a UTC timestamp is exposed or compared is converted via `ts_to_local_date`:

- **`get_available_dates`** — converts store timestamps to local date strings. The manifest always returns values the client can round-trip back unchanged as request dates.
- **`load_slice`** — iterates all timestamps in the store's `time` coordinate, converts each via `ts_to_local_date`, and collects those that match the requested date string exactly. The first matching timestamp is selected with `sel(time=pd.Timestamp(matching[0]))`. If multiple timestamps map to the same local date (e.g. sub-daily data), a `DEBUG` message is logged and the first is used. If no timestamp maps to the requested local date, `FileNotFoundError` is raised with a message indicating that dates must be in `LOCAL_TZ` local time (not UTC). This avoids `method="nearest"` silently serving data from an adjacent day.

**Critical constraint** — `get_available_dates` and `load_slice` must always use the same `LOCAL_TZ` value. Changing one without the other causes dates to silently mismatch: the manifest returns dates the client cannot successfully request. `TILE_TIMEZONE` is the single source of truth; never hardcode a timezone string in either function.

### 9.3 Client contract

Dates in the API are **opaque keys**, not calendar dates in the client's local timezone. Clients must:

1. Fetch available dates from `/manifest`.
2. Pass those exact date strings back in tile/point requests.

Do not construct date strings from the client's local clock — the server interprets them as `TILE_TIMEZONE` local dates, and a client in a different timezone would produce strings that do not exist in the manifest.

### 9.4 Sub-daily data

The current API is day-granularity only. If a store has sub-daily resolution, multiple UTC timestamps will map to the same local date — `load_slice` logs a `DEBUG` message and returns the first. Supporting hourly queries would require changes to the URL structure and cache-key design; deferred until there is a concrete use case.

### 9.5 Coordinate name normalisation

On store open, `_open_store` in `services/store/registry.py` applies `COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}` to rename any uppercase coordinate names to lowercase. This happens once per store URL and is cached on the singleton. All downstream code (renderer, manifest, point endpoint) can assume `lat`/`lon`/`time` regardless of what the store uses natively.

If `lat`/`lon` are still missing after renaming, `_open_store` raises `ValueError` with a clear message rather than failing deeper in the pipeline.

---

# Part V — Caching & runtime

## 10. Caching strategy

This section covers the **server-side cache stack** (tile → S3). For **HTTP caching** (Cache-Control headers, ETag revalidation, CACHE_VERSION invalidation through browsers and CloudFront), see [`docs/http_caching.md`](http_caching.md) — a separate concern with its own design.

Three-tier cache stack ordered tiles → S3: **L1 (in-memory processed grid, LRU) → L2 (in-memory slice, LRU) → L3 (disk) → S3**. Both in-memory tiers use `cachetools.LRUCache` — when the cache reaches its configured maximum size, the **least-recently-accessed entry is evicted** to make room. Visual tiles have no L1 — requests hit L2 first. Cold S3 reads (~2 s) are absorbed by disk (L3, ~30 ms) and the in-memory LRUs (L2 / L1, < 1 ms). The disk cache is the primary mechanism for eliminating cold origin hits — it persists across server restarts and is pre-populated at startup.

> Full design rationale (why disk over Redis / EFS / Fargate ephemeral): [`docs/cache_analysis.md`](cache_analysis.md).

### 10.1 Store singleton (`services/store/registry.py`, `StoreRegistry`)

Caches the open Zarr store handle (lazy, metadata only). Shared across all products that point at the same store URL.

Uses a **stale-while-revalidate** strategy to pick up newly appended time steps without ever blocking a request:

- **Startup** — `prewarm_stores` opens every registered store concurrently on the shared anyio pool, gated by `_STORE_PREWARM_LIMITER` (`STORE_PREWARM_WORKERS`, default 8), so the cache is warm before the first request arrives. Tracked as an `asyncio.Task` in the lifespan and cancelled cleanly on shutdown.
- **Within TTL** — the cached store is returned immediately (sub-millisecond).
- **After TTL** (`STORE_TTL_SECONDS`, default `600`) — the stale store is returned immediately for the current request, and a single background daemon thread calls `StoreRegistry._refresh_background` to re-open it. The `StoreRegistry._refreshing` set prevents duplicate refresh threads for the same URL.
- **First-ever open** — the request blocks until `xr.open_zarr` completes; concurrent requests for the same URL wait on the same `concurrent.futures.Future` rather than each opening independently. The Future is keyed per-URL in `StoreRegistry._in_flight`, so opens of _different_ URLs proceed in parallel.

Re-opening is cheap — `xr.open_zarr` reads only metadata and coordinate arrays (`time`, `lat`, `lon`), no data chunks. In-flight `load_slice` calls hold a direct Python reference to the old dataset object and complete normally. `_slice_cache` and `_processed_cache` entries for existing dates remain valid and unaffected.

Alongside the dataset, the registry builds a per-URL `{local_date: [timestamps]}` index (`_build_date_index`) so `load_slice` / `get_available_dates` can resolve a local date in O(1) instead of converting every timestamp on the hot path.

### 10.2 L1 — Processed grid cache (`services/caching/processed_cache.py`, `_processed_cache`)

Keyed `(source_path, date, str(variable), lod)`. Stores the resampled + normalised numpy arrays for the **full LOD grid**, not per-tile. A hit reduces per-tile work to `_extract_chunk` + PNG encode only — no S3 I/O, no resampling. The key is semantic (not object identity), so cache hits survive L2 slice evictions and disk-reloaded slices.

Entry sizes for the satellite heatwave product (2000×3900): LOD 1 ~1.4 MB, LOD 2 ~3.3 MB, LOD 3 ~12 MB, LOD 4 ~41 MB. GSLA-class products have only 1 LOD level at ~1.4 MB.

**Eviction.** `_processed_cache = TTLCache(maxsize=PROCESSED_CACHE_SIZE, ttl=PROCESSED_CACHE_TTL_SECONDS)` — entries are dropped when either constraint fires:

- **LRU at capacity.** When full, the least-recently-accessed `(product, date, lod)` entry is evicted. Active dates stay warm; cold ones get pushed out first.
- **TTL after insertion.** Each entry expires `PROCESSED_CACHE_TTL_SECONDS` after insertion (default 600 s / 10 min). Idle RAM returns to baseline after the user moves on; an unusually long stationary session pays one re-resample (~10–50 ms) when the entry first expires.

After eviction, the next request for that key recomputes the processed grid from the L2 slice (~tens of ms) or from L2 → disk → S3 if L2 has also evicted it.

Size is controlled by `PROCESSED_CACHE_SIZE` (default `50`). Sized as `SLICE_CACHE_SIZE × LOD.max_lods` with headroom: `10 × 4 = 40`, rounded to 50. This keeps all LOD levels warm for every date in the L2 slice cache.

On product deletion, `evict_processed_cache` is called from `evict_product_cache` in `caching/lifecycle.py` to purge all entries matching `source_path`.

Visual tiles do not use L1 — `XarrayReader` handles its own rendering per request from the L2 slice.

### 10.3 L2 — Slice cache, in-memory (`services/caching/slice_cache.py`, `_slice_cache`)

Keyed `(store_url, date, variables_tuple)`. Stores a fully-computed (`.compute()`) 2-D lat×lon `xr.Dataset` slice. Sub-millisecond on hit. Keyed by `variables_tuple` so different products using the same store cache independently.

**Eviction.** `_slice_cache = TTLCache(maxsize=SLICE_CACHE_SIZE, ttl=SLICE_CACHE_TTL_SECONDS)` — entries are dropped when either constraint fires:

- **LRU at capacity.** When the cache is full, the least-recently-accessed `(store_url, date, variables)` slice is evicted on the next insert. This is what bounds peak RAM under burst pressure (e.g. many concurrent map views on different `(product, date)` pairs).
- **TTL after insertion.** Each entry expires `SLICE_CACHE_TTL_SECONDS` after insertion (default 600 s / 10 min). This is what bounds idle RAM: after a user moves on from a date, the slice expires automatically instead of squatting in the LRU until something newer pushes it out.

Why this shape: L2's real job is to absorb the trailing tiles of a single map view. MapboxGL fires ~50–200 tile requests for the same `(product, date)` in a burst; in-flight dedup (`_slice_memo`) coalesces the simultaneous ones into a single L3 load, and L2 then serves the trailing arrivals over the next few seconds. After the user navigates away, the slice has no further reuse — TTL evicts it; LRU would keep it indefinitely.

L2 eviction (either path) does not invalidate the on-disk L3 copy. A subsequent request reloads from disk in ~30 ms via `pickle.loads(lz4.frame.decompress(...))`. The "thrash" cost of an undersized L2 is therefore a one-time ~30 ms disk-warm hit per re-request, not a full ~2 s cold S3 fetch.

Size is controlled by `SLICE_CACHE_SIZE` (default `10`). Entry size varies significantly by product: ~2 MB for a GSLA-class slice (351×641), ~61 MB for a satellite-class slice (2000×3900 float64).

Primary consumers are **visual_tiles** (no L1 above it — every tile request calls `load_slice`) and **data_tiles manifest/point** (always need `ds` directly). For data_tiles tile requests, the slice is only loaded on an L1 miss; once the processed grid is warm, L2 is bypassed entirely.

### 10.4 L3 — Slice cache, disk (`DISK_CACHE_PATH` directory)

Persists fully-computed slices as lz4-compressed pickles to survive server restarts. On an L2 miss, `load_slice` checks disk before going to S3. A disk hit (~30 ms read + decompress) is ~60× faster than a cold S3 fetch.

**Why lz4 + pickle?**

- **Pickle** is used because slices are `xr.Dataset` objects — preserving the full structure (coords, attrs, dtypes) on round-trip is the point, and pickle is the only stdlib option that handles xarray's nested numpy + dict-of-attrs layout without custom (de)serialisation code.
- **lz4** is chosen for **speed over ratio**: compress and decompress at ~500 MB/s on a single core, with ~3–4× compression on float64 ocean arrays. Contiguous NaN bit patterns in land masks compress especially well — for some products effective ratios are higher.

The (de)compression overhead is dwarfed by the size savings. A typical 18 MB compressed satellite slice decompresses to 61 MB in ~25 ms — all of which still fits inside the ~30 ms disk-hit budget that is ~60× faster than the ~2 s cold S3 fetch it replaces. Trade-offs avoided by this choice:

| If we used…                            | Cost                                                                                              |
| -------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Raw pickle, no compression             | ~3.4× more disk per product (60 GB instead of 18 GB for 10 satellite × 90 days), no read speed-up |
| gzip / zstd-max instead of lz4         | Better ratio (~1.5×) but compress/decompress ~5–10× slower — disk-hit budget exceeds cold S3      |
| Custom binary format instead of pickle | Loses xarray metadata or requires per-slice serialisation glue; no measurable compression win     |

Per-product compression ratios, lz4-vs-zstd-vs-snappy measurements, and the disk-vs-Redis-vs-EFS decision live in [`docs/cache_analysis.md`](cache_analysis.md).

File layout: `{DISK_CACHE_PATH}/{store_name}-{var_str}/{date}.pkl.lz4`.

Always enabled; path is the hardcoded constant `DISK_CACHE_PATH = "slice_cache"` (relative to the working directory — resolves to `/app/slice_cache` in Docker).

**Eviction:**

- _Stale dates_ — `evict_stale_and_orphans` (and the refresh cycle) deletes `.pkl.lz4` files whose dates are no longer in the `CACHE_DAYS` window for any registered product.
- _Orphan product directories_ — `evict_stale_and_orphans` removes any sub-directory under `DISK_CACHE_PATH` whose name does not correspond to a currently-registered product. This is what makes runtime product deletion safe: leftover disk slices from removed products are cleaned up automatically on the next refresh (and at startup).
- _Disk pressure_ — `evict_if_over_threshold` in `services/caching/disk.py` (run at the start of each refresh cycle) removes files when total usage exceeds `DISK_EVICTION_THRESHOLD × DISK_CACHE_LIMIT_GB`. Files are sorted `(size ascending, date ascending)` — small + old files are evicted first, keeping the large satellite slices that would be most expensive to re-fetch.
- _Explicit product deletion_ — `evict_product_cache` in `services/caching/lifecycle.py` (called by `DELETE /admin/products/{id}`) removes the product's disk directory via `shutil.rmtree` and purges matching entries from the L2 in-memory cache immediately.

Disk eviction never invalidates L2 in-memory entries — the in-memory data is still valid and serves requests until it falls out of the LRU naturally.

### 10.5 Stampede protection

All three layers use `concurrent.futures.Future` to deduplicate concurrent misses on the same key. The slice and processed-grid layers use the shared `Memoizer` helper (`utils/memoizer.py`), which packages the "check cache → create Future → wait → publish → cleanup" pattern in one place. The store layer keeps its own per-URL Future map inside `StoreRegistry` because it layers TTL + stale-while-revalidate on top of dedup, which the generic helper deliberately does not model.

- `StoreRegistry._in_flight` — store opens.
- `_slice_memo` (Memoizer over `_slice_cache`) — slice loads (`load_slice`).
- `_processed_memo` (Memoizer over `_processed_cache`) — processed grid computation (`_get_processed`).
- `_tile_memo` / `_bbox_memo` (Memoizers with `cache=None`) — dedup-only protection in front of the visual-tile renderer.

The first thread to miss the cache creates the Future and does the work; all other threads arriving for the same key block on `future.result()` and receive the same result when the single computation completes. Errors propagate to all waiting threads so a failed request does not permanently block future attempts for the same key. See [§12.8](#128-per-request-capacity-origin-server-ec2ecs-in-region) for capacity implications.

### 10.6 Cache-state visibility (`GET /admin/cache`)

A single read-only admin endpoint surfaces everything a production debugger usually wants to know about the cache. It returns five sections:

- **`disk`** — global L3 footprint: total bytes, configured limit (`DISK_CACHE_LIMIT_GB`), eviction threshold, utilisation %, and an `over_eviction_threshold` flag so pressure is visible at a glance.
- **`disk_writes`** — live state of the two background disk writers: `prewarm.running` (boolean) and `refresh` (status `never_run`/`running`/`ok`/`error`, start + completion timestamps, last error message, configured interval). Useful for spotting a stalled refresh or a prewarm still in progress after startup.
- **`in_flight`** — instantaneous count of computations currently mid-flight in each Memoizer (`current`), high-water mark since process start (`peak`), and total computes started since startup (`total_computes`). Slice and processed-grid memoizers reported separately. **Not** a rolling window — values reflect the moment of the request and drop to 0 when nothing is running.
- **`memory_cache`** — current size and max for the slice and processed-grid LRUs.
- **`products`** — per-product breakdown: disk file count, total bytes, oldest/newest cached date, most recent write mtime, and in-flight counts attributed by `(source_path, sorted_variables)` so two products sharing a Zarr don't collide.

The handler is a sync `def` (FastAPI dispatches it to the anyio pool automatically) and the filesystem walk is single-pass — every cache file is stat'd once, then attributed to a product via parent-dir-name lookup in `services/caching/disk.collect_disk_stats` — so a thousand-file cache costs at most a few milliseconds of thread-pool time and never blocks tile requests. Peak/total counters are bumped under the Memoizer's existing lock; ambient cost on the tile-serving path is two integer ops per cache miss.

### 10.7 In-memory cache flush (`DELETE /admin/cache/memory`)

Drops every entry from both in-memory tiers — `_processed_cache` (L1) and `_slice_cache` (L2) — and returns the counts cleared as `{"cleared": {"slice": N, "processed": M}}`. Disk (L3) is untouched, so the next request for any flushed key still hits disk in ~30 ms instead of re-fetching from S3.

Implemented as `Memoizer.evict_matching(lambda _: True)` in both `services/caching/slice_cache.clear_slice_cache` and `services/caching/processed_cache.clear_processed_cache`. Per-product fan-out for a specific deleted product is `evict_product_cache` in `services/caching/lifecycle.py`. The eviction runs under the Memoizer's existing lock — concurrent `get_or_compute` calls block briefly while keys are removed. Any **in-flight** compute is not cancelled: it finishes and re-publishes into the (now-empty) cache when it completes, so a flush during heavy load may leave a handful of partially-refilled entries from work that was already running. Callers that arrived before the flush and are still `await`ing a shared Future receive the original result, not a re-computed one — there is no torn state.

Use cases: forcing a re-read after a Zarr store has been updated out-of-band, recovering from a poisoned cache entry, or freeing memory during an interactive debugging session without restarting the process.

### 10.8 Disk cache flush (`DELETE /admin/cache/disk`)

Deletes every `.pkl.lz4` slice file from the L3 disk cache directory and returns the count removed as `{"cleared": {"disk": N}}`. Memory caches (L1, L2) are untouched.

If `prewarm_disk_slices` or `refresh_disk_cache` is currently running the endpoint returns **409 Conflict** immediately without touching the disk — both operations write slice files, so clearing during either is pointless. Wait for the operation to complete before retrying.

Implemented in `services/caching/disk.clear_disk_cache` — iterates the per-product subdirectories under `DISK_CACHE_PATH` and removes each with `shutil.rmtree`, leaving the base directory itself intact. The handler is a sync `def`, so FastAPI dispatches it to the anyio pool automatically and the event loop stays free. In-flight computes are not cancelled — they may re-populate the disk cache on completion.

Use cases: forcing a full cold repopulation from S3 (e.g. after the underlying Zarr data has been rewritten), recovering disk space during debugging, or resetting a corrupted cache state without restarting the server. After a flush, the next request for any date will fall through to S3 (~2 s) and the disk cache will be repopulated gradually — or trigger `prewarm_disk_slices` via a server restart to repopulate in bulk.

---

## 11. Background tasks

The server runs two long-lived background tasks scheduled on the event loop at startup, plus several ad-hoc background actions. None of them block request handling — see [§12](#12-concurrency-event-loop-and-threading) for why.

### 11.1 Lifespan overview (`main.py`)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = int(os.environ.get("THREAD_POOL_SIZE", 100))

    load_products()                      # sync: read products.json into PRODUCTS dict
    load_colormaps()                     # sync: read colormaps.json into the colormap registry

    store_urls = list({p.source_path for p in PRODUCTS.values()})
    store_prewarm_task = asyncio.create_task(prewarm_stores(store_urls))   # anyio pool, gated by _STORE_PREWARM_LIMITER

    prewarm_task = asyncio.create_task(_startup_cache_sync(list(PRODUCTS.values())))
    interval = int(os.environ.get("CACHE_REFRESH_INTERVAL_SECONDS", 14400))
    refresh_task = asyncio.create_task(_cache_refresh_loop(interval))

    yield  # ← server handles requests here

    for task in (store_prewarm_task, prewarm_task, refresh_task):
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
```

Everything before `yield` runs on startup; everything after runs on shutdown. **The server begins handling requests immediately at `yield`** — it does not wait for prewarm or refresh to finish. Both background tasks pause at `await` points so the event loop is free for incoming requests.

### 11.2 `prewarm_task` — startup cache sync (one-shot)

Wraps `_startup_cache_sync(products)`, which does two sequential phases:

1. **`evict_stale_and_orphans(products)`** — a sync function called via `await anyio.to_thread.run_sync(evict_stale_and_orphans, products)`, so the filesystem walk runs off the event loop. It removes
   - any cached `.pkl.lz4` files for dates outside the `CACHE_DAYS` window for each product, **and**
   - any cache sub-directory whose name doesn't match a currently registered product (orphans left over after a product was removed in a previous run).

   This ensures the disk cache reflects the current product/date state from the moment the server starts serving, not just after the first refresh cycle.

2. **`prewarm_disk_slices(products)`** — itself `async def`; awaited directly, it manages its own offload. For each `(product, date)` pair in the last `CACHE_DAYS` dates, calls `_prewarm_one` (sync). Pairs are fanned out via `anyio.create_task_group` where the spawn loop acquires `_PREWARM_SEM` (`anyio.Semaphore(PREWARM_WORKERS)`, default 8) _before_ `tg.start_soon`, so at most N tasks exist at once — bounding both S3 fan-out and the pending-coroutine count when product × date is large. Disk-cached pairs return instantly; missing ones are fetched from S3 and written to disk. The task group exits only once every job has finished. The trailing `evict_if_over_threshold` runs inside the same `try` so `is_prewarm_running()` stays `True` through the unlink pass and `DELETE /admin/cache/disk` cannot race it.

If eviction fails for any reason it is logged and prewarm proceeds anyway — partial cache is better than no cache.

The task completes once; it is not periodic.

### 11.3 `refresh_task` — periodic cache refresh (long-running)

`_cache_refresh_loop(interval)` runs in an infinite `while True` loop:

```python
async def _cache_refresh_loop(interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_disk_cache(list(PRODUCTS.values()))
        except Exception:
            logger.exception("Cache refresh cycle failed; will retry next interval")
```

Key properties:

- **Re-reads `PRODUCTS` every cycle** — products added or removed via the admin API are picked up automatically on the next tick; no restart required.
- **Broad exception handling** — an unhandled exception inside the loop would kill it for the lifetime of the process and silently disable all future refreshes. The broad `except` keeps the loop alive across transient failures.
- **`asyncio.sleep` yields to the event loop** — other tasks and requests run freely during the wait.
- **`refresh_disk_cache` is `async def` and offloads internally** — its sync work (eviction, S3 fetches, `.compute()` calls) is wrapped in `anyio.to_thread.run_sync` per phase, so the event loop stays free without the caller needing a `to_thread` wrapper.

`refresh_disk_cache` itself:

1. Calls `evict_stale_and_orphans(products)` first — drops files outside the date window and removes orphan product directories.
2. For each product, computes the target window (`get_available_dates[-CACHE_DAYS:]`) and writes any missing date's slice to disk via `load_slice` + `lz4.frame.compress(pickle.dumps(ds))`.

Default interval is `CACHE_REFRESH_INTERVAL_SECONDS = 14400` (4 hours). In steady state on IMOS daily data this adds ~1 new date per product per cycle and evicts ~1 stale date.

### 11.4 Other background actions

| Trigger                     | Action                                                                                       | Mechanism                                                                                                                                             |
| --------------------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `prewarm_stores` at startup | Open each unique Zarr store URL (metadata only) so first requests don't pay the cost         | `asyncio.Task` fanning out on the anyio pool, gated by `_STORE_PREWARM_LIMITER` (`STORE_PREWARM_WORKERS`, default 8); tracked + cancelled at shutdown |
| Store TTL expiry            | Re-open Zarr store in the background to pick up new timestamps; stale store served meanwhile | `StoreRegistry._refresh_background` via `threading.Thread`                                                                                            |
| `POST /admin/products`      | Prewarm the disk cache for the newly registered product                                      | `asyncio.create_task(prewarm_disk_slices([product]))` — `prewarm_disk_slices` is `async def` and offloads internally                                  |
| `DELETE /admin/products`    | Evict the product's in-memory L1/L2 entries and remove its disk directory                    | Synchronous on the request thread (fast — file delete + dict pop)                                                                                     |

### 11.5 Graceful shutdown

On shutdown (Uvicorn signal handler), the lifespan `finally` block:

- `cancel()`s `store_prewarm_task`, `prewarm_task`, and `refresh_task`.
- `await`s each one to handle `asyncio.CancelledError` cleanly.
- Logs any other exception that escaped.

`task.cancel()` aborts the _await_ — any `xr.open_zarr` or `_prewarm_one` already running in an anyio worker thread runs to completion (CPython threads are uncancellable). Anyio worker threads themselves are released back to the shared pool when their job returns; there is no separate pool to drain. Daemon threads used by store TTL refresh (`StoreRegistry._refresh_background`) do not need explicit cleanup — they exit with the process.

---

## 12. Concurrency: event loop and threading

The server combines an **asyncio event loop** (for FastAPI/Uvicorn request multiplexing and the lifespan's background tasks) with a **single bounded thread pool** (anyio's, for all CPU- and I/O-heavy work). Three named budgets carve that one pool into independent slices — one for each background fan-out (`_PREWARM_SEM`, `_ANIMATION_LIMITER`, `_STORE_PREWARM_LIMITER`) — so background work cannot starve request serving. Understanding which work runs where is essential when reasoning about latency, throughput, and capacity.

### 12.1 Why most endpoints are `def`, not `async def`

Look at the route definitions in `routers/`:

```python
@router.get("/{product_id}/{date}/{z}/{x}/{y}.{ext}")
def get_tile(...):
    ...
```

These are **synchronous** `def` functions, not `async def`. FastAPI/Starlette inspects each handler at registration time and routes sync handlers to a thread pool managed by `anyio` (the same `anyio.to_thread.current_default_thread_limiter()` whose `total_tokens` we set to `THREAD_POOL_SIZE` in the lifespan).

The reason is twofold:

1. **`xarray` / `zarr` / `rio-tiler` are blocking libraries.** None of them expose async read APIs. A call to `ds.sel(...).compute()` blocks until the S3 chunks are downloaded and decompressed; a call to `XarrayReader.tile(...)` blocks until reprojection finishes. If we wrote these handlers as `async def`, every blocking call would freeze the event loop — every request would queue up behind whichever one happened to be fetching from S3 (potentially seconds).
2. **PNG encoding, numpy resampling, and lz4 decompression are CPU-bound.** Even ignoring I/O, the actual work per tile is non-trivial (a satellite LOD-4 grid is 41 MB to allocate, normalise, and pack). Doing that on the event loop would block every other request for the duration.

By defining handlers as plain `def`, each one runs on a worker thread from the anyio pool. The event loop stays responsive: it only does the work of accepting connections, parsing HTTP headers, dispatching to handlers, and serialising responses.

#### When does `async def` actually help?

A recurring confusion: "if I split a tile pipeline into `await load(); await process(); await encode()`, does it run faster?" The honest answer is **no for a single request**, and understanding why clarifies when `async def` is and isn't worth reaching for.

There are two distinct kinds of "parallelism" that get conflated:

| Kind                           | What it is                                    | Who provides it                                                                                                                      |
| ------------------------------ | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **Within-request parallelism** | One request's internal steps run concurrently | Only `async def` + `asyncio.gather` (or task-group `start_soon`) over **independent** steps. Useless for sequential/dependent steps. |
| **Across-request concurrency** | The server handles many requests at once      | Both `def` (via the anyio thread pool) and `async def` (via the loop + thread pool). Same outcome either way.                        |

For a sequential blocking pipeline (`load → process → encode`, where each step needs the previous step's output), `await` enforces ordering — the next step starts when the previous one finishes, identical to plain function calls. Wrapping it as `async def` with multiple `to_thread.run_sync` hops adds extra thread-acquisition overhead with no wall-clock win. This is why tile handlers stay `def`.

`async def` wins in three concrete cases:

1. **Independent fan-out** — `/animation` reads N frames in parallel via `asyncio.gather`; total time drops from `N × per_frame` to `~max(per_frame)`. Needs `async def`.
2. **Truly async I/O** (none used here today) — see below.
3. **Selective offload boundaries** — keep cheap parsing/validation on the loop, offload only the heavy step. `get_animation`'s prelude is the example.

#### What async I/O actually buys (if we ever introduce it)

For sync I/O via `to_thread.run_sync`, a slow S3 read holds a **worker thread** for the entire wait, doing nothing but sleeping on a socket. With a truly-async client (`aiobotocore`, an async HTTP library, etc.), `await async_get(...)` releases the thread during the wait — the coroutine parks on the event loop, costing only a small Python object.

```
Sync-in-thread:   thread held for whole 2 s S3 wait    ████████████████████
Async I/O:        thread held only during CPU work     ▏▏  (~0.1 s)
```

Within ONE request, wall-clock is the same — `await` still enforces ordering. The benefit shows up at the **system level**:

- Pool size 10, S3 wait 2 s, processing 0.1 s.
- **Sync model**: 10 concurrent requests fill all 10 threads, each idle on a socket. An 11th request queues for ~2.1 s. Throughput ≈ 5 req/s.
- **Async S3 model**: 1000 concurrent requests can be parked on the loop waiting on S3; threads are only consumed during the 0.1 s processing phase. Throughput limited by processing, not by I/O wait. Throughput ≈ 100 req/s.

The thread pool stops being the bottleneck during I/O wait.

**Why we don't currently benefit from this.** `xarray.open_zarr` → `fsspec` → `urllib3`/`botocore` is sync end-to-end. There is no `await` to insert; the only option is `to_thread.run_sync(...)`, which holds a thread for the duration. To get the async-I/O win we would have to bypass xarray and implement chunk fetching against `aiobotocore` directly. Worth considering only if S3-wait-holding-threads becomes a measurable bottleneck — see [§12.9](#129-scaling-thread_pool_size).

#### The rule of thumb for this codebase

- Single sequential blocking pipeline → `def`. Simpler, one thread hop instead of three.
- Independent work to fan out → `async def` + `asyncio.gather` / `create_task_group`.
- Need a running event loop in the handler (e.g. to `asyncio.create_task` a background job, like `add_product`) → `async def`, with the blocking part offloaded via `to_thread.run_sync`.
- Mixing async I/O with sync CPU work → `async def`, splitting at the async/sync boundary.

### 12.2 The thread pool

```python
limiter = anyio.to_thread.current_default_thread_limiter()
limiter.total_tokens = int(os.environ.get("THREAD_POOL_SIZE", 100))
```

The pool has `THREAD_POOL_SIZE` slots (default 100). Each in-flight sync request occupies one slot from the start of the handler to its return. The Python GIL means only one thread executes CPU-bound Python at a time, but:

- **I/O releases the GIL** — `xarray`'s S3 fetch is mostly `urllib3`/`botocore` socket I/O. While one thread waits on S3, others can run.
- **numpy/PIL release the GIL during their C-level work** — resampling, normalisation, and PNG encoding all benefit from real parallelism.

Stampede protection (`_slice_memo`, `_processed_memo`, `StoreRegistry._in_flight`) means that if 10 requests arrive for the same cold key, only 1 thread does the work; the other 9 hold their slots blocked on the Future. This caps peak unique work and peak RAM, but the held slots do count toward `THREAD_POOL_SIZE`. See [§12.8](#128-per-request-capacity-origin-server-ec2ecs-in-region) and [§12.9](#129-scaling-thread_pool_size) for the full capacity analysis.

### 12.3 Background tasks run on the event loop and offload work via `anyio.to_thread.run_sync`

The two `asyncio.create_task(...)` calls in `lifespan` create coroutines that run on the event loop:

- `_startup_cache_sync` — awaits `anyio.to_thread.run_sync(evict_stale_and_orphans, products)` (eviction is sync), then `await prewarm_disk_slices(products)` (prewarm is itself `async def` and handles its own offload internally).
- `_cache_refresh_loop` — awaits `asyncio.sleep(interval)`, then `await refresh_disk_cache(...)` directly.

Each `await` is a yield point: the event loop is free to dispatch other tasks (including incoming HTTP requests) until the awaited operation completes. The blocking work itself (S3 fetches, disk reads, `.compute()`) runs on a thread from the anyio pool — it does **not** run on the event loop.

This is why a 60-second prewarm at startup does not delay the first request by 60 seconds. The event loop yields at each `await anyio.to_thread.run_sync(...)` boundary, the prewarm work runs in the background, and the event loop continues to handle requests on other threads.

`prewarm_disk_slices` and `refresh_disk_cache` further parallelise across `(product, date)` pairs using `anyio.create_task_group` with `_PREWARM_SEM` (`anyio.Semaphore(PREWARM_WORKERS)`, default 8) acquired _before_ each `tg.start_soon`. The semaphore-gated spawn pattern caps both the number of concurrent S3 fetches **and** the number of pending coroutines — for a thousand-job prewarm we never hold more than ~8 coroutines alive. Crucially, this is a **separate concurrency budget** from the default tile-handler limiter — prewarm jobs do not consume slots that tile handlers would otherwise use. See [§12.6](#126-one-pool-three-named-budgets) for why.

### 12.4 Quick reference

| Component                         | Runs on                                                                                                                                             | Why                                                                                                                                                                                                                                                                          |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| HTTP accept / parse / route       | Event loop                                                                                                                                          | Pure async I/O; never blocks                                                                                                                                                                                                                                                 |
| Tile/manifest/point handlers      | Anyio pool, default limiter (`THREAD_POOL_SIZE`)                                                                                                    | Sync `def` so blocking xarray/rio-tiler/PIL calls don't freeze the loop                                                                                                                                                                                                      |
| `/animation`                      | Event-loop coroutine → `anyio.to_thread.run_sync` per frame, gated by `_ANIMATION_LIMITER` (`ANIMATION_WORKERS`, default 10)                        | `async def` so per-frame `load_slice_uncached` calls fan out via `asyncio.gather`; latency drops to ~max(per-frame) instead of the serial sum. Limiter keeps a many-frame request from monopolising the tile-handler budget — see [§12.6](#126-one-pool-three-named-budgets) |
| `/admin/products` POST            | Event loop (`async def`); `register_product` JSON write offloaded via `anyio.to_thread.run_sync`; spawns `prewarm_disk_slices` as a background task | Stays `async def` because `_spawn_prewarm` calls `asyncio.create_task` and that needs a running loop in this thread                                                                                                                                                          |
| `/admin/products` DELETE          | Event loop (sync `def`)                                                                                                                             | In-memory removal + sync cache eviction                                                                                                                                                                                                                                      |
| `GET /admin/cache`                | Anyio pool, default limiter (sync `def`)                                                                                                            | Filesystem walk dispatched automatically by FastAPI                                                                                                                                                                                                                          |
| `DELETE /admin/cache/disk`        | Anyio pool, default limiter (sync `def`)                                                                                                            | Filesystem walk dispatched automatically by FastAPI                                                                                                                                                                                                                          |
| `/products`, `/colormaps` listing | Event loop (`async def`)                                                                                                                            | In-memory dict reads only                                                                                                                                                                                                                                                    |
| Store prewarm at startup          | Anyio pool, gated by `_STORE_PREWARM_LIMITER` (`STORE_PREWARM_WORKERS`)                                                                             | Concurrent metadata fetches, tracked as `asyncio.Task`                                                                                                                                                                                                                       |
| Store TTL refresh                 | One daemon thread per URL on TTL expiry                                                                                                             | Stale store returned immediately; fresh open happens in the background                                                                                                                                                                                                       |
| `_startup_cache_sync`             | Event-loop task; `evict` via `anyio.to_thread.run_sync`; `prewarm` is `async def` awaited directly                                                  | Coroutine yields while the actual eviction + prewarm work runs on threads                                                                                                                                                                                                    |
| `_cache_refresh_loop`             | Event-loop task; `refresh_disk_cache` is `async def` awaited directly                                                                               | Coroutine yields during sleep + refresh; never blocks the loop                                                                                                                                                                                                               |
| `prewarm_disk_slices` fan-out     | Anyio pool, spawn-gated by `_PREWARM_SEM` (`PREWARM_WORKERS`)                                                                                       | At most N tasks alive at a time — bounds S3 fan-out and pending coroutines                                                                                                                                                                                                   |
| In-flight stampede dedup          | Anyio pool (callers block on `Future`)                                                                                                              | Holds a slot but does no work — see §12.2                                                                                                                                                                                                                                    |

### 12.5 Failure modes to watch

- **`async def` an endpoint by accident.** If a future contributor turns a `def` handler into `async def`, blocking calls inside it (any `xarray`/`rio-tiler` call) will freeze the event loop and serialise every request behind the slowest one. There is no static check for this — review carefully.
- **Forget `anyio.to_thread.run_sync` inside an `async def` function.** `prewarm_disk_slices`, `refresh_disk_cache`, and the admin coroutines all run on the event loop. Any blocking call inside their body — `get_available_dates`, `evict_stale_and_orphans`, `evict_if_over_threshold`, filesystem walks — must be wrapped in `anyio.to_thread.run_sync(...)` or it freezes the loop. The `async def` shape converts a one-time offload (at the call site of a sync function) into a per-call discipline (every blocking line inside the body); review additions to these functions carefully.
- **Unbounded background tasks.** Both lifespan tasks have a top-level `try/except`. New background tasks must do the same — an unhandled exception in an `asyncio.Task` is silent until the task is awaited.
- **Saturate `_PREWARM_SEM` with the wrong workload.** The semaphore is sized to the S3 connection ceiling for slice fetches (`PREWARM_WORKERS=8`). Don't acquire it around filesystem ops, metadata reads, or anything else not bound by that resource — semantically wrong and accidentally serialises unrelated work.

### 12.6 One pool, three named budgets

§12.2 talks about "the thread pool" singular. That framing is accurate at the OS level: nearly every offload in this app lands in **one** anyio worker pool. What's split into independent slices is the **concurrency budget** on that pool — three named gates, each sized to its own bottleneck. Two are `anyio.CapacityLimiter` (acquired inside `to_thread.run_sync`); one is `anyio.Semaphore` (acquired _before_ `tg.start_soon` to also bound the pending-coroutine count).

#### Why a single pool, not three

Earlier versions of this server used three thread pools:

1. anyio's pool for sync `def` tile handlers (size 100).
2. asyncio's default executor for `asyncio.to_thread(...)` calls (~32) — used by admin endpoints and background tasks.
3. A `concurrent.futures.ThreadPoolExecutor(max_workers=PREWARM_WORKERS)` built inside `prewarm_disk_slices` for the S3 fan-out (size 8).

The three-pool layout bought isolation but at the cost of conceptual overhead — three different APIs (`anyio.to_thread.run_sync`, `asyncio.to_thread`, manual `ThreadPoolExecutor`), three different sizing knobs, and the subtle pitfall that `asyncio.to_thread` and `anyio.to_thread.run_sync` look interchangeable but route to **different pools**. Reviewers had to keep that distinction in mind on every async-related change.

The current design collapses this to one pool with one default limiter and three named feature budgets:

- **Default limiter** (size `THREAD_POOL_SIZE`, default 100) — the limiter `anyio.to_thread.current_default_thread_limiter()` returns. Used by every sync `def` tile handler (dispatched automatically by FastAPI) and by every `anyio.to_thread.run_sync(...)` call that doesn't pass an explicit limiter. Admin GET/DELETE endpoints also fall under this budget.
- **`_PREWARM_SEM`** (size `PREWARM_WORKERS`, default 8) — a module-level `anyio.Semaphore` in `services/caching/lifecycle.py`. Acquired _before_ `tg.start_soon` in `prewarm_disk_slices` / `refresh_disk_cache` so at most N tasks (and thus N pending coroutines) exist at a time. Sized to the S3 connection-pool ceiling.
- **`_ANIMATION_LIMITER`** (size `ANIMATION_WORKERS`, default 10) — a module-level `anyio.CapacityLimiter` in `routers/visual_tiles.py`. Used by the per-frame `load_slice_uncached` fan-out inside `/animation`, via the explicit `limiter=_ANIMATION_LIMITER` argument. Sized to the aiobotocore S3 connection-pool ceiling (~10/host); going higher just queues on the connection pool without reducing latency, and the bound keeps a many-frame request from monopolising the tile-handler budget.
- **`_STORE_PREWARM_LIMITER`** (size `STORE_PREWARM_WORKERS`, default 8) — a module-level `anyio.CapacityLimiter` in `services/store/registry.py`. Used by `StoreRegistry.prewarm` to gate concurrent `xr.open_zarr` opens at startup. Same S3 connection-pool rationale as `_PREWARM_SEM`.

All four budgets live over the **same** anyio worker pool. Anyio creates worker threads on demand and they're shared across budgets — but each call only acquires (or pre-acquires) the budget it was given, so the slices are independent. A prewarm burst saturating its 8-slot budget does not reduce the tile-handler budget of 100, and a 30-frame animation does not steal from prewarm either.

#### Semaphore vs CapacityLimiter — when to use which

Both bound concurrent work, but they live at different points in the spawn lifecycle:

- **CapacityLimiter passed to `to_thread.run_sync(..., limiter=...)`** — the coroutine is _already created_ when the limiter is acquired. For fan-outs built via `asyncio.gather(*(to_thread.run_sync(...) for ...))`, all N coroutines exist simultaneously, each parked on the limiter. Fine when N is small and bounded (≤30 frames for animation, ≤handful of stores for prewarm).
- **Semaphore acquired before `tg.start_soon`** — the spawn loop pauses on `sem.acquire()` when N workers are running, so at most N coroutines exist _at all_. Necessary when N could be large (`prewarm_disk_slices` over `products × CACHE_DAYS` is thousands).

`_PREWARM_SEM` is a Semaphore because product × date jobs can grow into the thousands. `_ANIMATION_LIMITER` and `_STORE_PREWARM_LIMITER` are CapacityLimiters because their N is bounded (30 frames; one open per unique store URL).

#### Why three named budgets, not one

| Concern                                               | What the layout buys                                                                                                                                                                                                                                                                                                          |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Tile traffic isolated from background work**        | A 1000-slice prewarm doesn't consume tile-handler capacity. Without `_PREWARM_SEM`, the same fan-out would either flood the default 100-slot limiter (starving requests) or keep thousands of coroutines alive parked on a limiter.                                                                                           |
| **Tile traffic isolated from animation requests**     | A 30-frame `/animation` request consumes at most 10 anyio slots, not all 100. Without `_ANIMATION_LIMITER`, a couple of concurrent animation requests could saturate the tile budget.                                                                                                                                         |
| **Tile traffic isolated from store-prewarm bursts**   | An admin registering 50 distinct sources won't briefly consume 50 pool threads on startup — `_STORE_PREWARM_LIMITER` caps it at 8.                                                                                                                                                                                            |
| **Resource ceilings decoupled from request capacity** | `PREWARM_WORKERS=8`, `ANIMATION_WORKERS=10`, `STORE_PREWARM_WORKERS=8` all match the aiobotocore S3 connection-pool ceiling (~10/host) — the real bottleneck for S3 work. All are independent of how many tile requests can run concurrently.                                                                                 |
| **Admin endpoints answer under tile-pool pressure**   | `/admin/cache` GET and `DELETE /admin/cache/disk` are sync `def` and run under the default limiter — they share the 100-slot budget with tile handlers, but admin endpoints are one-shot filesystem walks; the practical chance of contention is negligible, and the simplicity is worth more than a fourth dedicated budget. |

#### Non-pool worker threads (unchanged)

- **Store TTL refresh daemon threads** — `StoreRegistry._refresh_background` spawns a bare `threading.Thread` per stale-store re-open. Lives outside the anyio pool because it's triggered from inside `get()`, which may itself be running in a worker thread without an event loop reference. Threads exit when each open completes. Not a reusable pool.
- **C-extension threads** — Zarr decompression, NumPy via BLAS, and PIL/libpng all release the GIL and may use their own internal threads. Total OS thread count is always higher than the sum of the Python-managed threads above.

#### Convention: which to use where

- **Inside a `def` handler** — already on the anyio pool under the default limiter. Don't offload further unless the work is unusually heavy and would block other tile requests.
- **Inside an `async def` function** — wrap any blocking call in `anyio.to_thread.run_sync(...)`. By default this runs under the default limiter (shared with tile handlers, which is fine for one-shot ops). For bounded-N batch fan-out, pass an appropriate `limiter=` (or define a new module-level `CapacityLimiter` sized to _that_ workload's bottleneck). For unbounded-N fan-out, use `anyio.create_task_group` + `anyio.Semaphore` with `await sem.acquire()` _before_ `tg.start_soon` to bound coroutine memory too. Don't reuse `_PREWARM_SEM` / `_ANIMATION_LIMITER` / `_STORE_PREWARM_LIMITER` for unrelated work — see §12.5.
- **For background coroutines** (prewarm, refresh, store prewarm) — call them with plain `await`. They're `async def` and handle their own offload via `anyio.to_thread.run_sync` / task-group fan-out internally.

The trade-off compared to the old three-pool design: admin endpoints and the refresh loop no longer have a dedicated executor isolated from tiles. In exchange, the mental model is much simpler — one pool, named budgets where isolation matters. Tile-vs-prewarm, tile-vs-animation, and tile-vs-store-prewarm isolation, the properties that actually matter for capacity planning, are preserved by their dedicated budgets.

### 12.7 Per-request paths

Every tile request falls into one of four paths depending on which cache layer it hits. Latency and which resources it consumes vary by an order of magnitude across the paths — this taxonomy is the basis for the capacity tables in §12.8.

**Data tile paths (`/data_tiles/...`).** `load_slice` is lazy — the route handler passes a callable to `render_tile`, which only invokes it if `_get_processed` misses:

- **Processed warm** — `(product, date, lod)` already in `_processed_cache`. The thread does `_extract_chunk` + PNG encode only — no S3, disk, or slice I/O.
- **Slice warm** — `_processed_cache` misses; `(product, date)` is in the L2 slice cache. The thread loads `ds` from memory, resamples, populates `_processed_cache`, then encodes.
- **Disk warm** — `_processed_cache` and L2 both miss; `(product, date)` is on disk. The thread reads + decompresses the lz4 pickle (~30 ms), resamples, populates both caches, then encodes.
- **Cold** — nothing cached. The thread fetches Zarr chunks from S3 (`.compute()`, ~2 s), writes to disk and L2, resamples, populates `_processed_cache`, then encodes.

**Visual tile paths (`/visual_tiles/...` and `/bbox`).** No processed grid cache. Each request calls `load_slice` unconditionally:

- **L2 warm** — `(product, date)` in L2. Reads `ds` from memory and renders via `XarrayReader`.
- **Disk warm** — L2 miss; slice on disk. Reads + decompresses, populates L2, renders.
- **Cold** — fetches from S3, writes to disk and L2, renders.

All paths share the anyio thread pool and compete for the same slots. Processed-warm data-tile requests are fastest and release their slot quickly; cold requests hold slots for seconds.

### 12.8 Per-request capacity (origin server, EC2/ECS in-region)

S3 latency from within the same AWS region is an internal network hop — effectively negligible compared to home internet. The dominant cost on a cold request is chunk decompression and numpy assembly, not network wait.

**Hot requests** (processed-warm or L2-warm):

| Factor           | Value                            |
| ---------------- | -------------------------------- |
| Request duration | ~10–50 ms                        |
| Max simultaneous | 100 (thread pool limit)          |
| Throughput burst | ~100 ÷ 0.03 s ≈ **3,000 req/s**  |
| Bottleneck       | CPU (PNG encode) and thread pool |

**Disk-warm requests:**

| Factor           | Value                                                    |
| ---------------- | -------------------------------------------------------- |
| Request duration | ~50 ms (GSLA-class) — ~200 ms (satellite-class)          |
| Max simultaneous | 100 (thread pool limit)                                  |
| Throughput burst | ~2,000 req/s (GSLA-class) — ~500 req/s (satellite-class) |
| Bottleneck       | EBS read + lz4 decompress + numpy resample               |

Sustained throughput on satellite-class slices is further capped by **EBS bandwidth**: gp3 baseline 125 MB/s ÷ 18 MB/slice ≈ ~7 unique satellite-slice loads/sec. Provision EBS IOPS / bandwidth above baseline if disk-warm becomes the dominant traffic pattern (rare in practice — repeated reads of the same slice promote it to L2 after the first hit, after which the request is hot).

**Cold requests (S3):**

| Factor                       | Value                                                    |
| ---------------------------- | -------------------------------------------------------- |
| Request duration             | ~400 ms (GSLA-class) — ~1.5–2 s (satellite-class)        |
| Max simultaneous cold slices | 100 (thread pool limit; deduplicated by `_slice_memo`)   |
| Throughput burst             | ~250 req/s (GSLA-class) — ~50–70 req/s (satellite-class) |
| Bottleneck                   | S3 fetch + CPU (decompression + numpy resample)          |

The dominant cost on cold-class requests is the **S3 fetch itself** (~300–800 ms per Zarr chunk; the satellite-class slice needs 6 chunks). Disk-warm is ~5–10× faster than cold because the same 18 MB slice reads from local EBS (~5–25 ms) instead of S3, and is stored fully assembled rather than as 6 separate Zarr chunks needing recombination.

In practice cold S3 requests only occur for dates older than `CACHE_DAYS` (outside the disk-cache window) or before startup prewarm completes. The hot / disk-warm / cold numbers above are **per-request** and independent of product mix — they hold for Scenario A, B, and C alike. What changes across scenarios is the **hit-rate distribution**: a larger product mix increases the chance that any given request falls into a cold or disk-warm tier rather than the hot tier, which is why [§14.3](#143-why-the-default-slice_cache_size10-is-too-small-for-production) sizes cache capacity to keep the working set hot.

### 12.9 Scaling `THREAD_POOL_SIZE`

The throughput numbers in §12.8 are **burst ceilings** computed as `THREAD_POOL_SIZE ÷ request_duration` at `THREAD_POOL_SIZE = 100`. They represent what the pool can absorb in a brief spike, **not what the server can sustain indefinitely**. Sustained throughput is bound by real resources (CPU cores, EBS bandwidth, S3 connection pool) that don't scale with thread count.

**What scales with `THREAD_POOL_SIZE`, and what doesn't:**

| Resource                              | Scales with pool size? | Actual ceiling                                                                                           |
| ------------------------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------- |
| Burst capacity (short spikes)         | **Yes**, linearly      | Transient RAM: `pool_size × 61 MB` worst-case unique satellite slices in flight                          |
| Hot sustained throughput              | **No**                 | CPU. On 4 vCPU with GIL-releasing PNG encode, plateaus around **~250–400 req/s** regardless of pool size |
| Disk-warm sustained (satellite-class) | **No**                 | EBS bandwidth: gp3 baseline 125 MB/s ÷ 18 MB ≈ **~7 unique slices/sec**                                  |
| Cold sustained (satellite-class)      | Partially              | S3 connection pool (aiobotocore default ~10 per host) + CPU for decompress/assembly                      |
| Queueing tolerance under burst        | **Yes**, linearly      | OS thread limit (Linux defaults: thousands per process)                                                  |

**Throughput at higher `THREAD_POOL_SIZE` (burst ceilings):**

| `THREAD_POOL_SIZE` | Hot burst     | Disk-warm satellite burst | Cold satellite burst | Worst-case transient RAM | Thread-stack RAM |
| ------------------ | ------------- | ------------------------- | -------------------- | ------------------------ | ---------------- |
| 50                 | ~1,500 req/s  | ~250 req/s                | ~25–35 req/s         | ~3 GB                    | ~50 MB           |
| **100** (default)  | ~3,000 req/s  | ~500 req/s                | ~50–70 req/s         | ~6 GB                    | ~100 MB          |
| 200                | ~6,000 req/s  | ~1,000 req/s              | ~100–140 req/s       | ~12 GB                   | ~200 MB          |
| 500                | ~15,000 req/s | ~2,500 req/s              | ~250–350 req/s       | ~30 GB                   | ~500 MB          |

Burst columns scale linearly because they're arithmetic ceilings, not physical ones. **Sustained throughput converges to the CPU / EBS / S3 ceilings regardless of pool size** — raising the pool from 100 to 500 with 4 vCPU does not give 5× sustained hot throughput; it just lets bursts of 500 concurrent requests be absorbed without queueing rejections, at the cost of 5× transient RAM.

**Theoretical maximum.** `anyio.to_thread.current_default_thread_limiter().total_tokens` accepts any positive integer — anyio has no hard cap. Practical ceilings are OS-level:

- **`ulimit -u`** (max user processes) — usually thousands, configurable.
- **RAM stack**: ~1 MB per thread (Linux default `pthread` stack). 1000 threads ≈ 1 GB.
- **GIL** + **vCPU**: at most `N_cores × ~5` concurrent threads produce real CPU throughput; the rest are blocked on I/O or context-switched.

**When to raise the pool size:**

- Nginx access logs show request latency spikes correlated with concurrent-request count → the pool is exhausted, raise it.
- Steady-state CPU is **< 70 %** on all cores while you observe queueing → the pool, not the CPU, is the bottleneck.
- CPU is pegged at **100 %** across all cores → CPU is the bottleneck; raising the pool just adds context-switching overhead. Provision more vCPU or scale out horizontally instead.

For the production scenarios in [§14.7](#147-planning-scenarios), `THREAD_POOL_SIZE = 100` is sufficient when fronted by CloudFront (§12.10), which absorbs the bulk of repeat traffic before it reaches the origin. Raise to 200 only when sized for a workload that legitimately produces simultaneous bursts of >100 unique uncached requests and you have the RAM headroom.

### 12.10 CloudFront and real-world concurrency

In production, CloudFront sits in front of this server and caches tile responses at the edge. A tile URL (`/visual_tiles/{product}/{date}/{z}/{x}/{y}.png`) is fully deterministic — the same URL always returns the same bytes for a given product and date — so CloudFront's cache hit rate is very high once a date has been requested.

In practice:

- The vast majority of tile requests are served by CloudFront and never reach the origin.
- Only cache misses (first request for a tile coordinate, or after CloudFront TTL expiry) hit the origin.
- The thread pool and stampede protection (§10.5) are a **backstop for origin misses**, not the steady-state load path.

Concurrency pressure on the origin is therefore much lower than the theoretical maximums in §12.8 / §12.9 suggest.

---

# Part VI — Operations

## 13. Adding a new product

`products.json` is the single source of truth for the product list. The server reads it once on startup (`load_products()` in `services/product/registry.py`) and exposes a runtime admin API that reads and writes the same file. Two equivalent flows:

| Flow                                                             | When to use                                                                  | Effect                                                                                                                                |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| **Bootstrap** — write `products.json` before `docker-compose up` | Fresh deployment, infra-as-code, or any reproducible bootstrap.              | `load_products()` reads the file into `PRODUCTS` during lifespan startup; `_startup_cache_sync` then prewarms disk for every product. |
| **Admin API** — `POST /admin/products` to the running server     | Adding or removing products without a restart in an already-deployed system. | The admin handler appends to `products.json`, reloads `PRODUCTS`, and fires a background `prewarm_disk_slices` for the new product.   |

Both flows produce identical in-memory state. The bootstrap flow skips the admin-key + network round-trip but requires file-system access to `data/products.json`. The admin-API flow works once the server is running and is the only option in environments where you cannot touch the host filesystem.

### 13.1 Bootstrap flow — pre-populate `products.json`

For a fresh deployment, write the file before starting the container so the server comes up with products already registered. The Docker Compose default path is `data/products.json`:

```bash
mkdir -p data
cat > data/products.json << 'EOF'
[
  {"id": "sea_level_anomaly",                       "source_path": "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/", "variable": "GSLA",          "chunk_px": [240, 192], "padding": 1},
  {"id": "ocean_current",                           "source_path": "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/", "variable": ["UCUR","VCUR"], "chunk_px": [240, 192], "padding": 1},
  {"id": "satellite_austemp_heatwave_8day_ssta",    "source_path": "s3://aodn-cloud-optimised/satellite_austemp_heatwave_8day.zarr",           "variable": "ssta",          "chunk_px": [240, 192], "padding": 1}
]
EOF
docker-compose up --build
```

The schema mirrors the admin-API payload (`ProductPayload` in `routers/admin/products.py`). `chunk_px` and `padding` are optional — omit them to inherit `CHUNK_PX = (240, 192)` and `PADDING = 1` from `constants.py`.

### 13.2 Admin-API flow

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

- The store is opened and the declared `variable`(s) are checked against `store.data_vars`. A failure (unreachable bucket, malformed URL, missing variable) returns **400** and nothing is persisted. Without this guard, a typo'd URL would stay registered and the failure would only surface later in the background prewarm log. The validation runs synchronously via `anyio.to_thread.run_sync` because `xr.open_zarr` is blocking; on success, `get_store` caches the open dataset in the store singleton so the subsequent prewarm reuses it.
- `products.json` is written atomically (tempfile + `os.replace`) under a process-wide `threading.Lock`, so concurrent admin writes can't interleave and a crash mid-write can't leave a truncated file. `PRODUCTS` is then reloaded from the file.
- `evict_product_cache` is **not** called for new products (nothing to evict).
- A disk-cache prewarm for the new product is fired with `asyncio.create_task(prewarm_disk_slices([product]))` — `prewarm_disk_slices` is `async def` and offloads its sync work internally.

On the first request after registration:

- The store is opened and coordinates are normalised automatically.
- LOD grids are computed from the store's actual lat/lon dimensions (see [§7](#7-data-tile-internals)).
- Rendering and manifest generation work generically from `product.variable`.

On deletion:

- `products.json` is rewritten without the product, and `PRODUCTS` reloads.
- `evict_product_cache` removes the product's entries from `_slice_cache`, the L1 processed cache, and its disk directory.

### 13.3 Requirements for the Zarr store

| Requirement        | Detail                                                                                                                                                                                                  |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Coordinate names   | Must be `lat`/`lon`/`time`, or the uppercase variants `LATITUDE`/`LONGITUDE`/`TIME` (renamed automatically on open). If a store uses different names, add a mapping to `COORD_NAMES` in `constants.py`. |
| Spatial dimensions | `lat` and `lon` must be present after normalisation — `_open_store` raises `ValueError` with a clear message if not.                                                                                    |
| CRS                | Coordinates must be geographic degrees (EPSG:4326). The visual renderer guards against projected CRS values; see [§8.1](#81-crs-guard).                                                                 |
| Variable           | The variable(s) named in `Product.variable` must exist in the store.                                                                                                                                    |

### 13.4 Optional overrides

`Product` fields can be customised per product if the defaults don't fit:

| Field       | Default              | When to override                                          |
| ----------- | -------------------- | --------------------------------------------------------- |
| `chunk_px`  | `(240, 192)`         | Store has very small or very large spatial extent         |
| `padding`   | `1`                  | Tile edge artefacts, or no padding needed                 |
| `lod_grids` | `{}` (auto-computed) | Pre-set known grids to skip the first-request computation |

---

## 14. Capacity and resource planning

This section quantifies how RAM and disk grow with product count, slice size, thread-pool size, and cache size. Use it when picking instance class for a new deployment or sizing a horizontal scale-out.

### 14.1 Planning premise — what kinds of products do we plan for?

The products listed in [`docs/dataset.md`](dataset.md) are **representative examples**, not an exhaustive or fixed list. Actual production products are registered at runtime via the admin API and will vary over time, but they are expected to **stay close in shape and scale** to the examples documented there — same order of magnitude in grid size, same dtype, same regular lat/lon convention.

For capacity planning we abstract those examples into **two size classes** and treat every actual product as falling into one of them:

| Size class          | Anchored on (example in `dataset.md`) | Grid scale   | L2 slice in RAM | L1 processed (all LODs combined)  | L3 disk (lz4) per date |
| ------------------- | ------------------------------------- | ------------ | --------------- | --------------------------------- | ---------------------- |
| **GSLA-class**      | sea_level_anomaly / ocean_current     | ~351 × 641   | ~2 MB / var     | ~1.4 MB (single LOD)              | ~0.5 MB / var          |
| **Satellite-class** | satellite_austemp_heatwave_8day_ssta  | ~2000 × 3900 | ~61 MB          | ~58 MB (4 LODs, ~15 MB avg/entry) | ~18 MB                 |

A real product won't match these numbers exactly — a 400 × 700 product is still GSLA-class for sizing; a 1800 × 4200 product is still satellite-class. Use the closest class as the planning anchor; a product that is meaningfully different in scale (e.g. 5000 × 10000) needs a one-off calculation from [§14.2](#142-ram-components) before fitting into the scenarios below.

A production deployment is expected to be **dominated by satellite-class products** with a smaller number of GSLA-class accompaniments. `CACHE_DAYS` ranges from `30` (default) up to `90` (3-month history — the maximum the project plans to support). The three scenarios in [§14.7](#147-planning-scenarios) bracket what we expect to see in practice, each sized at all three cache windows:

| Scenario        | Products                    | Phase                         |
| --------------- | --------------------------- | ----------------------------- |
| **A — Initial** | 6 (2 GSLA + 4 satellite)    | Initial production deployment |
| **B — Steady**  | 20 (6 GSLA + 14 satellite)  | Mid-term steady state         |
| **C — Ceiling** | 50 (10 GSLA + 40 satellite) | Long-term single-node ceiling |

### 14.2 RAM components

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

### 14.3 Why the default `SLICE_CACHE_SIZE=10` is too small for production

With **10+ satellite products**, default `SLICE_CACHE_SIZE=10` gives you at most one cache slot per product. Any request for a non-cached date evicts another product's most recent slice — the cache thrashes and most visual-tile requests fall through to disk (or S3 on cold start). Two sizing principles:

- **At minimum**, size for one slot per product: `SLICE_CACHE_SIZE ≥ product_count`. With 10 satellite products that means **`SLICE_CACHE_SIZE = 10`** is the _floor_, not the recommended setting.
- **Recommended**, size for a few recent dates per product so users panning across recent dates stay in L2: `SLICE_CACHE_SIZE ≈ product_count × hot_dates_per_product`. For 10 products with ~3 hot dates each: **`SLICE_CACHE_SIZE = 30`**, **`PROCESSED_CACHE_SIZE = 120`** (i.e. `SLICE_CACHE_SIZE × LOD.max_lods`).

Memory cost of these recommendations: ~2.7 GB and ~3.9 GB steady respectively, before transient headroom. CloudFront mitigates the visible impact of L2 misses for repeat tile URLs but does not help requests for new dates.

### 14.4 How RAM scales when products are added

| Change                                                        | RAM impact                                                                                                                              |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Add a satellite-class product without changing cache sizes    | No new RAM ceiling, but L2/L1 hit rates degrade — more products compete for the same slots, more cold S3 reads.                         |
| Add a satellite-class product and raise `SLICE_CACHE_SIZE +1` | + ~61 MB L2, + ~58 MB L1 (one full row of LODs).                                                                                        |
| Raise `SLICE_CACHE_SIZE` by N (satellite worst case)          | + `N × 61 MB` L2, + `N × ~58 MB` L1.                                                                                                    |
| Raise `THREAD_POOL_SIZE`                                      | No direct steady RAM growth (~1 MB stack/thread). Higher _unique_ concurrent cold misses can spike transient RAM by `(N_cold) × 61 MB`. |
| Raise `PREWARM_WORKERS`                                       | Startup-only spike of `PREWARM_WORKERS × 61 MB`. Default 8 ≈ 500 MB.                                                                    |
| Raise `CACHE_DAYS` (e.g. 30 → 90)                             | **No effect on RAM** — only affects disk. L2/L1 sizes are bounded by their LRU sizes regardless of how many dates are on disk.          |

Stampede protection (`_slice_memo`, `_processed_memo`) means transient RAM scales with **unique cold keys in flight**, not `THREAD_POOL_SIZE`. But under truly mixed cold traffic (different `(product, date)` pairs from many users at once), the cap is `min(THREAD_POOL_SIZE, distinct_keys) × 61 MB`. With `THREAD_POOL_SIZE = 100` and a perfect-storm spread across many products and dates, that ceiling is **~6 GB** — short-lived but real. Provision RAM accordingly or lower `THREAD_POOL_SIZE`.

### 14.5 Thread pool vs cache sizing

Thread-pool size and cache size are **independent knobs** — concrete pairings are in the scenarios in §14.7. The general goal-to-knob mapping:

| Goal                                               | Knob                                                                         |
| -------------------------------------------------- | ---------------------------------------------------------------------------- |
| Serve more concurrent requests without queueing    | Raise `THREAD_POOL_SIZE` (cheap in steady RAM; raises transient ceiling)     |
| Keep more `(product, date)` pairs hot in RAM       | Raise `SLICE_CACHE_SIZE` (and `PROCESSED_CACHE_SIZE = SLICE_CACHE_SIZE × 4`) |
| Keep more dates available on disk without S3 reads | Raise `CACHE_DAYS` (affects disk only, not RAM)                              |
| Shorten startup prewarm duration                   | Raise `PREWARM_WORKERS`                                                      |

### 14.6 Disk usage formula

After lz4 compression, disk usage per scenario follows:

```
Disk total ≈ N_satellite × CACHE_DAYS × 18 MB
           + N_GSLA      × CACHE_DAYS × 0.5 MB  (typically < 5% of total)
```

The GSLA-class term is small enough to ignore for sizing decisions. The dominant variable is `N_satellite × CACHE_DAYS`. Pressure eviction triggers at `DISK_CACHE_LIMIT_GB × DISK_EVICTION_THRESHOLD` (default `20 × 0.85 = 17 GB`) — **the default 20 GB limit is too small for almost any production scenario** and must be raised explicitly. Eviction policy is documented in [§10.4](#104-l3--slice-cache-disk-disk_cache_path-directory).

EBS gp3 storage costs $0.08/GB-month, so disk is not a cost lever — capacity-planning correctness is. Plan for ~1.5× the steady-state disk total to absorb transient writes during refresh cycles.

### 14.7 Planning scenarios

Each scenario gives the disk footprint at the three cache windows (30 / 60 / 90 days) and the recommended cache sizing and instance class. **Cache RAM is independent of `CACHE_DAYS`** — only disk usage scales with the cache window, so the cache sizing column is shown once per scenario.

For each scenario, two cache-sizing strategies are presented:

- **1 hot date / product (floor)** — the minimum that prevents constant L2 eviction across products. Suitable when traffic is concentrated on a single recent date per product.
- **3 hot dates / product (recommended)** — absorbs users panning across recent dates without evicting a sibling product's slot. The default sizing the scenarios optimise for.

Steady RAM column = process baseline (~400 MB) + L2 cache worst-case (satellite-dominated) + L1 cache mixed-LOD typical. Add up to ~6 GB transient when `THREAD_POOL_SIZE = 100` and many distinct cold satellite slices arrive simultaneously (rare in practice due to CloudFront + stampede dedup, but the recommended instance has headroom for it).

#### Scenario A — 6 products (2 GSLA + 4 satellite)

Initial production deployment.

**Disk:**

| Metric                            | 30 days | 60 days | 90 days |
| --------------------------------- | ------: | ------: | ------: |
| Lz4 steady total                  | ~2.2 GB | ~4.4 GB | ~6.6 GB |
| Recommended `DISK_CACHE_LIMIT_GB` |       4 |       8 |      12 |
| EBS gp3 volume                    |    8 GB |   16 GB |   16 GB |

**Cache and RAM** (independent of `CACHE_DAYS`):

| Strategy                                | `SLICE_CACHE_SIZE` | `PROCESSED_CACHE_SIZE` | Steady cache RAM | Steady total |
| --------------------------------------- | -----------------: | ---------------------: | ---------------: | -----------: |
| 1 hot date / product (floor)            |                  6 |                     24 |          ~0.7 GB |      ~1.1 GB |
| **3 hot dates / product (recommended)** |                 18 |                     72 |          ~2.2 GB |      ~2.6 GB |

**Recommended instance:** `m6i.xlarge` (4 vCPU, **16 GB**). Comfortably absorbs ~2.6 GB steady plus up to ~6 GB transient cold burst. `m6i.large` (8 GB) is feasible only with `THREAD_POOL_SIZE` lowered to ~30 to cap transient RAM.

---

#### Scenario B — 20 products (6 GSLA + 14 satellite)

Mid-term steady state.

**Disk:**

| Metric                            | 30 days |  60 days |  90 days |
| --------------------------------- | ------: | -------: | -------: |
| Lz4 steady total                  | ~7.7 GB | ~15.3 GB | ~22.9 GB |
| Recommended `DISK_CACHE_LIMIT_GB` |      12 |       24 |       36 |
| EBS gp3 volume                    |   16 GB |    32 GB |    48 GB |

**Cache and RAM:**

| Strategy                                | `SLICE_CACHE_SIZE` | `PROCESSED_CACHE_SIZE` | Steady cache RAM | Steady total |
| --------------------------------------- | -----------------: | ---------------------: | ---------------: | -----------: |
| 1 hot date / product (floor)            |                 20 |                     80 |          ~2.4 GB |      ~2.8 GB |
| **3 hot dates / product (recommended)** |                 60 |                    240 |          ~7.3 GB |      ~7.7 GB |

**Recommended instance:** `m6i.2xlarge` (8 vCPU, **32 GB**) for the recommended 3-hot-date strategy — leaves ~24 GB headroom over the ~7.7 GB steady for transient bursts and OS overhead. The 1-hot-date strategy fits on `m6i.xlarge` (16 GB) if traffic is concentrated on the latest date per product.

---

#### Scenario C — 50 products (10 GSLA + 40 satellite)

Long-term ceiling for a single node. At this scale, **horizontal scale-out usually beats a single large node** on cost and resilience.

**Disk:**

| Metric                            |  30 days |  60 days |  90 days |
| --------------------------------- | -------: | -------: | -------: |
| Lz4 steady total                  | ~21.7 GB | ~43.5 GB | ~65.2 GB |
| Recommended `DISK_CACHE_LIMIT_GB` |       32 |       64 |       96 |
| EBS gp3 volume                    |    48 GB |    80 GB |   128 GB |

**Cache and RAM** (single-node sizing — see scale-out note below):

| Strategy                                | `SLICE_CACHE_SIZE` | `PROCESSED_CACHE_SIZE` | Steady cache RAM | Steady total |
| --------------------------------------- | -----------------: | ---------------------: | ---------------: | -----------: |
| 1 hot date / product (floor)            |                 50 |                    200 |          ~6.1 GB |      ~6.5 GB |
| **3 hot dates / product (recommended)** |                150 |                    600 |         ~18.2 GB |     ~18.6 GB |

**Recommended deployment options:**

- **Horizontal scale-out (preferred above ~30 products):** 2–3 × `m6i.xlarge` or `m6i.2xlarge` replicas behind CloudFront. Each replica has independent L1/L2/L3 caches but reads from the same S3 stores; CloudFront fans out at the edge. Cheaper, more resilient, and avoids the very-large-instance pricing curve. Each replica is sized per Scenario B numbers.
- **Single node:** `m6i.4xlarge` (16 vCPU, **64 GB**) for the recommended strategy, or `m6i.2xlarge` (32 GB) if 1 hot date per product is acceptable.

---

#### Prewarm time at startup

Cold startup `prewarm_disk_slices` grows linearly with `N_satellite × CACHE_DAYS`. At the default `PREWARM_WORKERS = 8` and ~3–4 s per satellite slice (S3 fetch + decompress + pickle + lz4 + write):

| Scenario         | 30 days | 60 days | 90 days |
| ---------------- | ------: | ------: | ------: |
| A (4 satellite)  |  ~1 min |  ~2 min |  ~3 min |
| B (14 satellite) |  ~3 min |  ~7 min | ~10 min |
| C (40 satellite) | ~10 min | ~20 min | ~30 min |

Raise `PREWARM_WORKERS` further (e.g. 12–16) to halve startup again at the cost of more transient RAM and S3 bandwidth contention. On warm restart (disk already populated), prewarm completes in seconds regardless of scenario — it just verifies files exist.

> Full capacity-per-request-type tables (hot / disk-warm / cold throughput per request) are in [§12.8](#128-per-request-capacity-origin-server-ec2ecs-in-region) and [§12.9](#129-scaling-thread_pool_size).

---

## 15. Environment variables

Consolidated reference. Defaults match the application code; the Docker Compose overrides in `docker-compose.yml` use the same defaults.

### 15.1 Configuration philosophy — where does a new tunable belong?

This codebase holds configuration in three places. Both env vars and code constants are evaluated once at startup, so from a "when does it take effect" perspective they are equivalent — the choice of layer is a deliberate **signal** about how a value should change, not a runtime distinction.

| Layer                                                | What lives here                                                                                          | Change discipline                                                               | Examples                                                                                           |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| **Env vars** (this section)                          | Operational knobs — perf, resource limits, secrets. Do **not** affect wire format or shader contract.    | Rotate freely at deploy; the value itself doesn't need code review.             | `THREAD_POOL_SIZE`, `SLICE_CACHE_SIZE`, `CACHE_DAYS`, `ADMIN_API_KEY`                              |
| **Code constants** (`constants.py`)                  | Wire / shader contracts — values that must stay in lockstep with the frontend or with the data encoding. | Change via PR so frontend and server stay in sync; the diff is the audit trail. | `LOD.max_lods`, `LOD.min_coarsest`, `LOD.zoom_thresholds`, `CHUNK_PX`, `PADDING` (global defaults) |
| **Per-product fields** (`Product` dataclass + admin) | Data characteristics that legitimately vary across products.                                             | Set per product via `POST /admin/products`; no code change needed.              | `chunk_px`, `padding`, `variable`, `source_path`                                                   |

**The rule when adding a new tunable**: ask _who needs to be informed when the value changes?_

- Only the operator → **env var**.
- The frontend (or any wire-format consumer) needs a matching update → **code constant**, so the change goes through code review alongside the frontend change.
- Only one product is affected → **per-product field**, exposed via the admin API.

A wrong-layer choice has real costs: making `max_lods` an env var would let an ops engineer raise it to `6` thinking "more LODs = better detail", silently overflowing the WebGL atlas's 4096×4096 (≈64 MB VRAM) cap and triggering LRU tile thrashing — rendering still works, but UX degrades through re-upload churn that ops can't easily diagnose without frontend context. Making `THREAD_POOL_SIZE` a code constant would require a redeploy and PR for every perf-tuning experiment.

### 15.2 Server

| Variable        | Default            | Description                                                                                 |
| --------------- | ------------------ | ------------------------------------------------------------------------------------------- |
| `TILE_TIMEZONE` | `Australia/Sydney` | IANA timezone for date conversion. See [§9](#9-date-timezone-and-coordinate-normalisation). |
| `ADMIN_API_KEY` | _(required)_       | Secret value compared against the `X-Admin-Key` header on every `/admin` request.           |

### 15.3 S3 client

Tuning knobs for the botocore client used by `fsspec`/`s3fs` underneath every Zarr read. The defaults match `docker-compose.yml`; assembled into a single `botocore.Config` at module import in `services/store/registry.py` and passed through `client_kwargs` to `fsspec` for every `s3://` URL.

| Variable             | Default | Description                                                                                                                                                                                                                                                            |
| -------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `S3_ANON`            | `true`  | When `true` (or any non-`false`/`0`/`no` value), uses anonymous access — correct for the public AODN buckets. Set to `false` to let `fsspec` discover AWS credentials via env vars, `~/.aws/credentials`, or the EC2 instance role.                                    |
| `S3_CONNECT_TIMEOUT` | `5`     | Seconds for DNS + TCP/TLS handshake. Bounds how long a stuck network state can pin a worker thread before failing — Python threads can't be cancelled, so without this an unreachable endpoint would hold the thread until the kernel eventually timed out (minutes).  |
| `S3_READ_TIMEOUT`    | `30`    | Seconds of socket inactivity before a read fails. **Per-read**, not per-request — multi-MB Zarr chunks are fine within 30s of continuous progress; the timeout only fires when the connection genuinely stalls. Pairs with `S3_MAX_ATTEMPTS` to retry transient blips. |
| `S3_MAX_ATTEMPTS`    | `2`     | Maximum total attempts (initial + retries) per S3 operation, using botocore's `standard` retry mode (exponential backoff on retryable errors). Keep low so a slow request fails fast instead of compounding cold-S3 latency across retries.                            |

### 15.4 Threading and cache sizing

| Variable                      | Default | Description                                                                                                                                                                                                    |
| ----------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `THREAD_POOL_SIZE`            | `100`   | Anyio thread-pool size. Each in-flight sync request uses one slot. See [§12](#12-concurrency-event-loop-and-threading).                                                                                        |
| `ANIMATION_WORKERS`           | `10`    | Capacity-limiter cap for `/animation` per-frame S3 fan-out. Sized to the aiobotocore S3 connection pool. See [§12.6](#126-one-pool-three-named-budgets).                                                       |
| `STORE_PREWARM_WORKERS`       | `8`     | Capacity-limiter cap for concurrent `xr.open_zarr` opens during startup store prewarm. Sized to the S3 connection pool. See [§12.6](#126-one-pool-three-named-budgets).                                        |
| `SLICE_CACHE_SIZE`            | `10`    | Max entries in the L2 in-memory slice cache. RAM bound: `SLICE_CACHE_SIZE × max_slice_size`.                                                                                                                   |
| `SLICE_CACHE_TTL_SECONDS`     | `600`   | Per-entry TTL for the L2 slice cache (`cachetools.TTLCache`). Entries expire this many seconds after insertion so idle RAM returns to baseline; `SLICE_CACHE_SIZE` still bounds capacity under burst pressure. |
| `PROCESSED_CACHE_SIZE`        | `50`    | Max entries in the L1 processed-grid cache. Sized as `SLICE_CACHE_SIZE × LOD.max_lods` with headroom.                                                                                                          |
| `PROCESSED_CACHE_TTL_SECONDS` | `600`   | Per-entry TTL for the L1 processed-grid cache (`cachetools.TTLCache`). Same idle-RAM rationale as `SLICE_CACHE_TTL_SECONDS`.                                                                                   |
| `STORE_TTL_SECONDS`           | `600`   | Stale-while-revalidate window for the Zarr store singleton.                                                                                                                                                    |

### 15.5 Disk cache (L3)

| Variable                         | Default | Description                                                                                                                                                                                                                |
| -------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DISK_CACHE_LIMIT_GB`            | `20`    | Maximum total disk usage before pressure-based eviction runs.                                                                                                                                                              |
| `DISK_EVICTION_THRESHOLD`        | `0.85`  | Fraction of limit at which pressure eviction triggers (0.0–1.0).                                                                                                                                                           |
| `CACHE_DAYS`                     | `30`    | How many recent dates per product to keep on disk; dates outside this window are evicted.                                                                                                                                  |
| `PREWARM_WORKERS`                | `8`     | Spawn-gate (`_PREWARM_SEM`) cap used during the startup disk prewarm and per-product prewarm fired by `POST /admin/products`. Bounds concurrent S3 fetches _and_ the pending-coroutine count when product × date is large. |
| `CACHE_REFRESH_INTERVAL_SECONDS` | `14400` | Period (seconds) between background refresh cycles. Default 4 hours.                                                                                                                                                       |

### 15.6 Logging

| Variable                       | Default  | Description                                                                                                                                                                      |
| ------------------------------ | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOG_FORMAT`                   | _(auto)_ | `json` — force JSON output. `text` — force human-readable. Unset (default) — JSON when stdout is not a TTY (containers, EC2, CI), human-readable when it is (local terminal).    |
| `LOG_LEVEL`                    | `INFO`   | Application log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Controls `services`, `routers`, and `main` namespaces. Uvicorn's own log level is set separately via `--log-level`. |
| `SLOW_FETCH_THRESHOLD_SECONDS` | `5`      | Log a `WARNING` when a cold S3 `.compute()` takes longer than this many seconds. See [§16.5](#165-operational-signals).                                                          |

See `docker-compose.yml` for the production wiring of these variables, and [`docs/security.md`](security.md) for how `ADMIN_API_KEY` interacts with nginx and the EC2 security group.

---

## 16. Logging

All logging configuration lives in `log_config.py`. `main.py` calls `configure_logging()` once at startup (after `load_dotenv()`) and nothing else touches logging setup.

### 16.1 Format selection

Format is chosen automatically from the TTY state of stdout — no configuration needed in most environments:

| Environment        | stdout TTY? | `LOG_FORMAT` | Format used                                    |
| ------------------ | ----------- | ------------ | ---------------------------------------------- |
| Local dev terminal | yes         | unset        | Human-readable (uvicorn default)               |
| Docker / EC2 / CI  | no          | unset        | JSON (one object per line)                     |
| Any                | —           | `json`       | JSON (forced)                                  |
| Any                | —           | `text`       | Human-readable (forced, e.g. `docker run -it`) |

JSON records share a single schema for both app logs and uvicorn access logs:

```json
{"time": "2026-05-19T06:02:50.073+00:00", "level": "INFO", "logger": "services.store.registry", "message": "Store opened", "store_url": "s3://bucket/sla.zarr", "date_count": 365}
{"time": "2026-05-19T06:02:51.210+00:00", "level": "ERROR", "logger": "main", "message": "Unhandled error", "method": "GET", "path": "/data_tiles/...", "exc": "Traceback ..."}
{"time": "2026-05-19T06:03:00.001+00:00", "level": "INFO", "logger": "uvicorn.access", "message": "...", "client_addr": "1.2.3.4:52100", "request_line": "GET /data_tiles/sla/2026-05-19/2/0/0.png HTTP/1.1", "status_code": 200}
```

### 16.2 Structured fields

`message` is a stable event name (e.g. `"Store opened"`, `"Slow S3 fetch"`); variable values are emitted as top-level JSON fields alongside it. This makes them queryable directly in CloudWatch Logs Insights without parsing the message string:

```
fields @timestamp, level, message, store_url, date, seconds
| filter message = "Slow S3 fetch" and seconds > 10
| sort @timestamp desc
```

Convention in code: pass values through `extra={...}` rather than `%s`-interpolating into the message.

```python
logger.info(
    "Store opened",
    extra={"store_url": store_url, "date_count": len(index)},
)
```

`JsonFormatter` promotes any non-stdlib attribute on the record (i.e. anything supplied via `extra={}`, plus the fields uvicorn attaches to access records) to a top-level JSON field automatically.

### 16.3 Application logger namespaces

Uvicorn's default `LOGGING_CONFIG` only wires `uvicorn.*` loggers to its handler. `configure_logging()` also routes `services`, `routers`, and `main` through the same handler so all application logs share one format and one destination.

### 16.4 Startup log sequence

A clean startup produces these `message` values in order (all `INFO` unless noted), each with its own structured fields:

```
Thread pool size set            thread_pool_size=100
Loaded products from disk       count=3, path=data/products.json
Loaded colormaps from disk      count=2, path=data/colormaps.json
Disk cache enabled              path=slice_cache, limit_gb=20, days=30, workers=8
Memory cache configured         slice_cache_size=10, processed_cache_size=50, store_ttl_seconds=600
Store opened                    store_url=s3://..., date_count=365             ← one per product, from prewarm threads
Cache refresh interval set      interval_seconds=14400
Prewarm complete                written=90, skipped=0, failed=0, seconds=23.4
```

If any line is missing, the corresponding feature is either misconfigured (missing env var) or failed silently — the prewarm/refresh error paths log at `WARNING` or `EXCEPTION` level.

### 16.5 Operational signals

Lines to watch for in production. Filter on `message` for the event name; the listed fields ride alongside it as queryable JSON.

| Level     | `message`                                                    | Key fields                                      | What it means                                                                                                                                                                             |
| --------- | ------------------------------------------------------------ | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WARNING` | `Slow S3 fetch`                                              | `store_url`, `date`, `seconds`                  | A cold `.compute()` exceeded `SLOW_FETCH_THRESHOLD_SECONDS`. S3 is slow for this key — check S3 region, VPC endpoints, or increase `DISK_CACHE_PATH` capacity so the date gets prewarmed. |
| `WARNING` | `Disk cache add failed`                                      | `product_id`, `date`                            | Refresh cycle couldn't write a slice — check disk space and `DISK_CACHE_LIMIT_GB`.                                                                                                        |
| `WARNING` | `Admin auth rejected: invalid key`                           | `client`, `path`                                | Failed admin authentication attempt. Unexpected `client` IPs warrant investigation.                                                                                                       |
| `DEBUG`   | `Multiple timestamps map to single date; first will be used` | `count`, `date`, `store_url`, `first_timestamp` | The Zarr store has more than one UTC timestamp resolving to the same local date (expected for sub-daily stores). The first timestamp is used. Enable `LOG_LEVEL=DEBUG` to see these.      |
| `ERROR`   | `Unhandled error`                                            | `method`, `path`                                | An uncaught exception reached the global handler — always signals a bug. The full traceback rides in `exc`.                                                                               |
| `ERROR`   | `Cache refresh cycle failed; will retry next interval`       | —                                               | The periodic refresh loop raised an unhandled exception. The next cycle still runs; check for disk-full or S3 access issues.                                                              |

Background task summary lines confirm normal operation:

```
Prewarm complete                 written=142, skipped=8, failed=0, seconds=23.4
Cache eviction complete          stale_dates=5, orphan_dirs=1
Refresh cycle complete           added=3, failed=0, seconds=4.1
Disk pressure eviction completed files_removed=12, mb_freed=480.0, usage_pct=71.2
```

These are always `INFO`. Absence of `Prewarm complete` after startup suggests a disk-cache config problem.

### 16.6 Health check suppression

`GET /health` responses are filtered from the uvicorn access log by `SuppressHealthChecks` (added in `configure_logging()`). Load-balancer probes fire every few seconds and would otherwise dominate the access log volume. App-level `/health` handler logs are unaffected — only the access-log entry is dropped.
