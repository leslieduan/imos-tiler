# Tile System

This project serves two independent tile APIs from the same underlying Zarr data: `data_tiles` and `visual_tiles`. They share the URL shape `/{product_id}/{date}/tiles/{z}/{x}/{y}.png` but `z`, `x`, `y` mean entirely different things in each.

---

## data_tiles — data-space grid

`z`, `x`, `y` are coordinates within the product's own geographic extent. There is no fixed world origin — `x=0, y=0` is the north-west corner of the dataset itself.

### `z` — LOD level

`z` is a Level-of-Detail index, not a Web Mercator zoom level. Valid values are the keys of `product.lod_grids` (e.g. `1`, `2`, `3`, `4`), where `1` is the coarsest and `N` is the finest. LOD grids are computed from the actual lat/lon dimensions of the Zarr store divided by a fixed tile size (`CHUNK_PX = (240, 192)`). See `technical.md` for the full algorithm.

### `x`, `y` — chunk column and row

- `x` = chunk column, `0` = westernmost
- `y` = chunk row, `0` = northernmost
- Valid range: `0 ≤ x < grid_cols`, `0 ≤ y < grid_rows` for the requested LOD (from `product.lod_grids[z]`)

Requesting an out-of-range tile returns **HTTP 404**.

### Manifest

The manifest endpoint (`/{product_id}/{date}/manifest.json`) provides the geographic bounds, LOD grid dimensions, and value ranges needed to map these coordinates back to real-world positions.

---

## visual_tiles — Web Mercator XYZ

`z`, `x`, `y` are standard Web Mercator tile coordinates — identical to OpenStreetMap, MapboxGL, and Leaflet.

### `z` — zoom level

At zoom `z`, the whole world is divided into a `2^z × 2^z` grid of tiles.

### `x`, `y` — tile column and row

- Valid range: `0 ≤ x, y ≤ 2^z - 1` (full globe)
- Requesting out-of-range values returns **HTTP 400**

Tiles that are geographically outside the product's data extent return a **transparent 256×256 PNG** (not an error), so the client can request the full world grid without needing to know the data bounds.

---

## visual_tiles bbox endpoint

`visual_tiles` also exposes `/{product_id}/{date}/bbox` which is **not** part of the tile pyramid. It renders an arbitrary Web Mercator bounding box (EPSG:3857) as a single PNG — the same rendering logic as the XYZ endpoint but using `rio-tiler`'s `reader.part()` instead of `reader.tile()`. This is for WMS-style consumers such as Mapbox GL's `{bbox-epsg-3857}` raster source placeholder, where the client manages the region directly rather than using a tile grid.

`data_tiles` has no equivalent — it only exposes the LOD pyramid.

---

## Comparison

| | `data_tiles` | `visual_tiles` |
|---|---|---|
| Coordinate space | Data-extent (product bounds) | World-space (Web Mercator) |
| `z` meaning | LOD index (1 = coarsest, N = finest) | Zoom level (0 = whole world) |
| `x`, `y` range | `0` to `grid_cols/rows - 1` per LOD | `0` to `2^z - 1` |
| Out-of-bounds response | HTTP 404 | Transparent PNG (spatial), HTTP 400 (invalid coords) |
| Multi-variable support | Yes (UV products) | No |
| Manifest | Yes | No |
