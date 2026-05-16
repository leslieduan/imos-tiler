# Tile System

This server exposes **two independent tile APIs** that share the URL shape `/{product_id}/{date}/tiles/{z}/{x}/{y}.png` but interpret `z`, `x`, `y` in entirely different coordinate systems. Mixing them up is the most common cause of "why is my tile blank / 404 / off-by-one" bugs.

| | `/data_tiles` | `/visual_tiles` |
|---|---|---|
| **Output CRS** | EPSG:4326 (Plate Carrée) | EPSG:3857 (Web Mercator) |
| **`z`, `x`, `y` meaning** | LOD level, chunk column, chunk row — anchored to the **product's own extent** | Standard slippy-map tile coordinates — anchored to the **whole world** |
| **Pixel content** | Raw values packed into RGBA bytes | Colourised image (after a colormap LUT) |
| **Consumer** | Custom WebGL shader (handles colour ramp + reprojection on the GPU) | Any map library that consumes XYZ Web Mercator tiles |
| **Multi-variable support** | Yes (UV vector products) | No (single-variable products only) |

**Which one should I use?**

- Building a normal map with Mapbox GL, MapLibre, Leaflet, OpenLayers, etc., and you just need pretty raster tiles overlaid on a base map → **`/visual_tiles`**.
- Building a custom WebGL visualisation where the client needs the raw scientific values (e.g. dynamic colour ramps, client-side analysis, particle animation on UV data) → **`/data_tiles`**.

See [`technical.md` §5](technical.md#5-tile-coordinate-systems-and-projection-pipeline) for the full projection-pipeline explanation and the rationale for serving in two different CRSs.

---

## 1. `data_tiles` — product-extent LOD pyramid (EPSG:4326)

`z`, `x`, `y` are coordinates **within the product's own bounding box**, not a global grid. There is no fixed world origin — `(x=0, y=0)` is the north-west corner of the dataset itself.

```
GET /data_tiles/sea_level_anomaly/2024-02-24/tiles/1/0/0.png   ← LOD 1, NW chunk
GET /data_tiles/sea_level_anomaly/2024-02-24/manifest.json    ← bounds + LOD grids + value ranges
```

### 1.1 `z` — LOD (Level-of-Detail) index

`z` selects a **resolution level**, not a map-zoom level. The valid values are the keys of `product.lod_grids` (typically `1`–`4`):

- `z = 1` is the **coarsest** level — fewest tiles, each covering a wide area at low detail.
- `z = N` is the **finest** level — most tiles, each at native data resolution.

This is the standard tile-pyramid pattern: each coarser level halves the `(cols, rows)` of the level below it, so a higher-zoom map view fetches a higher LOD and zooming out fetches a lower one. The LOD grids are computed at server startup from each Zarr store's actual lat/lon dimensions and the fixed chunk size (`CHUNK_PX = (240, 192)`). See [`technical.md` §7](technical.md#7-lod-grid-system) for the full algorithm.

The client maps map-zoom to LOD via the universal `LOD_ZOOM_THRESHOLDS` returned in each level's `zoomThreshold` field.

### 1.2 `x`, `y` — chunk column and row within the LOD grid

- `x` = chunk column; `x = 0` is the westernmost column.
- `y` = chunk row; `y = 0` is the northernmost row.
- Valid range at LOD `z`: `0 ≤ x < grid_cols` and `0 ≤ y < grid_rows`, where `(grid_cols, grid_rows) = product.lod_grids[z]`.

Requesting `(z, x, y)` outside this range returns **HTTP 404** — the URL refers to a tile that does not exist for this product. Clients are expected to fetch the manifest first so they know the per-LOD grid dimensions.

### 1.3 Per-tile manifest

```
GET /data_tiles/{product_id}/{date}/manifest.json
```

Required for decoding raw data tiles. Returns:

- `bounds` — geographic extent in degrees (`lonMin`, `lonMax`, `latMin`, `latMax`).
- `lods[z]` — for each LOD: `grid` (cols × rows), `chunkPx`, `storedPx`, `padding`, optional `zoomThreshold`.
- `valueRange` (scalar products) or `uRange` / `vRange` (UV products) — needed to decode the normalised RGBA bytes back to physical values.

---

## 2. `visual_tiles` — Web Mercator XYZ (EPSG:3857)

`z`, `x`, `y` are **standard Web Mercator slippy-map tile coordinates** — identical to OpenStreetMap, MapboxGL, MapLibre, Leaflet, and OpenLayers. The reprojection from the source EPSG:4326 data to the EPSG:3857 output PNG happens server-side via `rio-tiler`'s `XarrayReader.tile(...)`.

```
GET /visual_tiles/sea_level_anomaly/2024-02-24/tiles/5/29/19.png?colormap=RdBu_r&rescale=-0.5,0.5
```

### 2.1 `z` — Web Mercator zoom level

At zoom `z`, the entire world is divided into a `2^z × 2^z` grid of tiles. `z = 0` is one tile covering the whole world; each step up doubles the number of tiles per axis.

### 2.2 `x`, `y` — tile column and row over the globe

- `x = 0` is the leftmost (westernmost) column; `y = 0` is the topmost (northernmost) row.
- Valid range: `0 ≤ x, y ≤ 2^z − 1`.
- Out-of-range coordinates (e.g. `x = 2^z`) return **HTTP 400** — the URL is malformed.
- In-range tiles that fall **outside the product's data extent** return a **transparent 256×256 PNG** (not an error). The client can request the full world grid without first checking the data bounds.

### 2.3 bbox endpoint (not part of the tile pyramid)

```
GET /visual_tiles/{product_id}/{date}/bbox?bbox=…&crs=EPSG:4326   (default)
GET /visual_tiles/{product_id}/{date}/bbox?bbox=…&crs=EPSG:3857   (Mapbox raster source)
```

Renders an arbitrary bounding box as a single PNG using the same colormap/rescale logic as the XYZ endpoint, but via `rio-tiler`'s `reader.part()` instead of `reader.tile()`. The bbox is interpreted in the CRS specified by `?crs=`:

- `EPSG:4326` (default) — `minx,miny,maxx,maxy` in geographic degrees.
- `EPSG:3857` — `minx,miny,maxx,maxy` in Web Mercator metres. Useful with Mapbox GL's `{bbox-epsg-3857}` raster-source placeholder.

The output PNG is always in Web Mercator regardless of input CRS. `data_tiles` has no equivalent endpoint — it exposes only the LOD pyramid.

---

## 3. Side-by-side comparison

|                                  | `/data_tiles`                                       | `/visual_tiles`                                          |
| -------------------------------- | --------------------------------------------------- | -------------------------------------------------------- |
| Output CRS                       | EPSG:4326 (Plate Carrée)                            | EPSG:3857 (Web Mercator)                                 |
| `z` meaning                      | LOD index (`1` = coarsest, `N` = finest)            | Zoom level (`0` = whole world)                           |
| `(x, y)` reference frame         | Product's own extent (NW corner = `0, 0`)           | Whole world (Web Mercator origin = `0, 0`)               |
| `(x, y)` range at level `z`      | `0` to `lod_grids[z] − 1`                           | `0` to `2^z − 1`                                         |
| Out-of-range `(z, x, y)`         | HTTP 404                                            | HTTP 400 (invalid coords); transparent PNG (spatially outside data) |
| Pixel encoding                   | Raw values in RGBA bytes (uint24 or U/V)            | Colourised RGBA after colormap LUT                       |
| Reprojection happens             | Client-side, in the WebGL shader (GPU)              | Server-side, by `rio-tiler.XarrayReader`                 |
| Multi-variable products (UV)     | Supported                                           | Not supported                                            |
| Per-tile decode manifest         | Required (`/manifest.json`)                         | Not applicable                                           |
| Products-availability `/manifest` | Yes (shared endpoint)                              | Yes (same shared endpoint)                               |
| Extra non-tile endpoint          | —                                                   | `/bbox` (arbitrary region, EPSG:4326 or EPSG:3857)       |

> Note: the products-availability `/manifest` endpoint (date listings) is mounted under both prefixes via `routers/products.py`. Only the **per-tile** `/{product_id}/{date}/manifest.json` (bounds + value ranges) is data-tiles-specific.
