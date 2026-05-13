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

FastAPI tile server for IMOS ocean data products built on **titiler-core**. Entry point is `main.py`. All tile serving goes through a single Zarr-backed stack — the legacy NetCDF stack has been removed.

### File structure

```
main.py                  ← mounts /tiles and /cog routers, CORS middleware
constants.py             ← Product dataclass, LOD algorithm, all product configs
routers/tiles.py         ← /tiles endpoints (tile, manifest, point)
services/loader.py       ← Zarr store singleton, slice cache, get_lod_grids
services/renderer.py     ← processed grid cache, chunk extract, PNG encode
docs/technical.md        ← architecture, LOD algorithm, caching strategy
docs/dataset.md          ← per-store variable/dimension/chunking reference
```

### Active products (`constants.py` → `PRODUCTS`)

| Product ID | Variable(s) | Store |
|---|---|---|
| `sea_level_anomaly` | GSLA | `model_sea_level_anomaly_gridded_realtime.zarr` |
| `ocean_current` | UCUR, VCUR | `model_sea_level_anomaly_gridded_realtime.zarr` |
| `radar_SouthAustraliaGulfs_wind_delayed_qc_wdir` | WDIR | `radar_SouthAustraliaGulfs_wind_delayed_qc.zarr` |
| `satellite_austemp_heatwave_8day_ssta` | ssta | `satellite_austemp_heatwave_8day.zarr` |

### URL contract

```
GET /tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png
GET /tiles/{product_id}/{date}/manifest.json
GET /tiles/{product_id}/{date}/point?lat=&lon=
```

`z` = LOD level, `x` = chunk column (0 = westernmost), `y` = chunk row (0 = northernmost). Not Web Mercator — custom geographic atlas grid.

### PNG encoding contract

Tiles are RGBA PNGs (`optimize=False`). Byte layout consumed by a WebGL shader:
- **24-bit scalar** (GSLA, SSTA, WDIR, etc.): R=high byte, G=mid byte, B=low byte of normalised uint24; A=ocean mask (255=ocean, 0=land).
- **UV current**: R=U normalised 0–255, G=V normalised 0–255, B=ocean mask×255, A=255.

Normalisation ranges are in `manifest.json` — fetch it before decoding tiles.

### LOD grids

Each product has `lod_grids: dict[int, tuple[int, int]]` mapping LOD level → (cols, rows). For Zarr products this is auto-computed from native store dimensions on first request via `Product._compute_lod_grids`. See `docs/technical.md` for the algorithm.

### Adding a new product

1. Add a URL constant and `Product(...)` entry in `constants.py`
2. Add it to `PRODUCTS`
3. Update `docs/dataset.md` with store variable/dimension/chunking info
