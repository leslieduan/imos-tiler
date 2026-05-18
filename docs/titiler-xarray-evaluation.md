# titiler.xarray Evaluation

## What titiler.xarray provides

`titiler.xarray` is a submodule of titiler designed for serving tiles from multidimensional datasets (Zarr, NetCDF). It provides pre-built endpoints that:

- Open a Zarr store and list available variables
- Serve standard XYZ Web Mercator tiles with colormaps applied server-side
- Return visual PNG/JPEG images ready for display in standard map libraries

## Why this project does not use it

### 1. Tile scheme mismatch

titiler.xarray serves **Web Mercator XYZ tiles** (EPSG:3857) — the standard scheme used by Google Maps, Leaflet, Mapbox. This project uses a **custom geographic atlas grid** (Plate Carrée / EPSG:4326) where tile coordinates represent LOD level, chunk column, and chunk row into the native lat/lon data grid.

Switching would require redesigning the entire tile coordinate system and all frontend tile-fetching logic.

### 2. PNG encoding is incompatible

titiler.xarray produces **visual tiles** — it applies a colormap server-side and outputs displayable RGB images. This project produces **data tiles** — raw scalar or UV values are encoded into pixel channels (uint24 across RGB, ocean mask in A) for a WebGL shader to decode client-side.

| | titiler.xarray | This project |
|---|---|---|
| PNG content | Visual pixels (colormap applied) | Raw data values (uint24 encoded) |
| Colormap | Server-side | Client-side in WebGL shader |
| A channel | Alpha (transparency) | Ocean mask |

These encodings are fundamentally incompatible. titiler.xarray has no mechanism to produce the custom byte layout this project's shader expects.

### 3. No manifest endpoint

The frontend WebGL shader requires a `manifest.json` per product per date containing:
- Geographic bounds (`lonMin`, `lonMax`, `latMin`, `latMax`)
- Value ranges (`valueRange`, `uRange`, `vRange`) for data decoding
- LOD grid dimensions per level
- Chunk pixel size and padding

titiler.xarray has no equivalent. The manifest is specific to this project's shader contract.

### 4. No custom LOD system

This project's LOD system derives a pyramid of chunk grids from the native data dimensions, with configurable `MAX_LODS`, `MIN_COARSEST_GRID`, and zoom thresholds. titiler.xarray uses standard map zoom levels tied to Web Mercator, which don't map to the native data resolution pyramid.

### 5. No point query endpoint

`GET /{product_id}/{date}/point?lat=&lon=` (mounted under both `/data_tiles` and `/visual_tiles`) returns the actual data value at a geographic point. titiler.xarray does not provide this.

## Summary

titiler.xarray is designed for standard map visualisation — colourmap applied on the server, tiles served into a Mercator map. This project is a data tile server for a custom WebGL visualisation system where the GPU handles projection, data decoding, and colour mapping client-side. The two approaches are architecturally incompatible on every axis that matters: tile scheme, PNG encoding, coordinate system, and API contract.

The project uses titiler as a FastAPI foundation (routing, OpenAPI) but the tile-serving logic is intentionally custom.
