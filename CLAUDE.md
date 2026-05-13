# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the server (development)
uv run uvicorn main:app --reload

# Add a dependency
uv add <package>

# Run tests / lint / type check
uv run pytest
uv run ruff check .
uv run mypy .
```

## Architecture

FastAPI tile server for IMOS ocean data products. Entry point is `main.py`. All tile serving goes through a Zarr-backed stack — the legacy NetCDF stack has been removed.

### File structure

```
main.py                        ← mounts /data_tiles, /visual_tiles, /admin routers; CORS; lifespan startup
constants.py                   ← Product dataclass, LOD algorithm, CUSTOM_COLORMAPS, PRODUCTS dict (runtime-populated)
products.json                  ← persisted product registrations (gitignored, written by admin API)
colormaps.json                 ← persisted custom colormap registrations (gitignored, written by admin API)
routers/
  data_tiles.py                ← /data_tiles — raw RGBA PNG tiles for WebGL shader
  visual_tiles.py              ← /visual_tiles — colourised Web Mercator XYZ tiles + bbox endpoint
  products.py                  ← shared: /products, /manifest, /{id}/{date}/point — included by both tile routers
  admin.py                     ← /admin — product and colormap management (X-Admin-Key protected)
services/
  loader.py                    ← Zarr store singleton, slice cache, get_lod_grids
  data_renderer.py             ← processed grid cache, chunk extract, PNG encode (data tiles)
  visual_renderer.py           ← Web Mercator tile + bbox render, colormap lookup (visual tiles)
  product_store.py             ← products.json read/write + in-memory PRODUCTS dict
  colormap_store.py            ← colormaps.json read/write + in-memory CUSTOM_COLORMAPS
docs/technical.md              ← architecture, LOD algorithm, caching, URL contract (full reference)
docs/dataset.md                ← per-store variable/dimension/chunking reference
```

### Active products

Products are runtime-managed via the admin API and stored in `products.json`. `PRODUCTS` in `constants.py` starts empty and is populated on startup. Default registered products:

| Product ID | Variable(s) | Store |
|---|---|---|
| `sea_level_anomaly` | GSLA | `model_sea_level_anomaly_gridded_realtime.zarr` |
| `ocean_current` | UCUR, VCUR | `model_sea_level_anomaly_gridded_realtime.zarr` |
| `radar_SouthAustraliaGulfs_wind_delayed_qc_wdir` | WDIR | `radar_SouthAustraliaGulfs_wind_delayed_qc.zarr` |
| `satellite_austemp_heatwave_8day_ssta` | ssta | `satellite_austemp_heatwave_8day.zarr` |

### URL contract

Both `/data_tiles` and `/visual_tiles` share these endpoints (via `routers/products.py`):
```
GET /{prefix}/products
GET /{prefix}/manifest?from=YYYY-MM-DD&to=YYYY-MM-DD
GET /{prefix}/{product_id}/{date}/point?lat=&lon=
```

Data tiles (custom geographic atlas grid — not Web Mercator):
```
GET /data_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png
GET /data_tiles/{product_id}/{date}/manifest.json
```

Visual tiles (standard Web Mercator XYZ, single-variable products only):
```
GET /visual_tiles/colormaps
GET /visual_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png?colormap=viridis&rescale=min,max
GET /visual_tiles/{product_id}/{date}/bbox?bbox=minx,miny,maxx,maxy&crs=EPSG:4326&width=256&height=256&colormap=viridis&rescale=min,max
```

Admin (requires `X-Admin-Key` header):
```
POST   /admin/products
DELETE /admin/products/{product_id}
POST   /admin/colormaps
DELETE /admin/colormaps/{name}
```

### PNG encoding contract

Data tiles are RGBA PNGs (`optimize=False`). Byte layout consumed by a WebGL shader:
- **24-bit scalar** (GSLA, SSTA, WDIR, etc.): R=high byte, G=mid byte, B=low byte of normalised uint24; A=ocean mask (255=ocean, 0=land).
- **UV vector** (ocean current): R=U normalised 0–255, G=V normalised 0–255, B=ocean mask×255, A=255.

Normalisation ranges (`valueRange` for scalar, `uRange`/`vRange` for UV) are in `manifest.json` — fetch it before decoding tiles.

### LOD grids

Each product has `lod_grids: dict[int, tuple[int, int]]` mapping LOD level → (cols, rows). Auto-computed from native store dimensions on first request via `product.apply_computed_lod_grids()`. See `docs/technical.md` for the algorithm.

### Adding a new product

Use the admin API — no code changes needed:

```bash
curl -X POST http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"id": "my_product", "source_path": "s3://bucket/store.zarr", "variable": "VAR_NAME"}'
```

Store requirements: coordinates named `lat`/`lon`/`time` (or uppercase variants auto-renamed), variable must exist in the store. See `docs/technical.md` for full details.
