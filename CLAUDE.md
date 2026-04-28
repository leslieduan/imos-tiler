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
```

No test suite exists yet.

## Architecture

This is a **FastAPI** tile server built on top of **titiler-core**. The entry point is `main.py`.

### Current state

`main.py` wires up a standard titiler COG (Cloud-Optimized GeoTIFF) router and adds CORS middleware. This is essentially the titiler scaffold.

### Planned tile generation layer

A custom on-demand tile generation system is being added for IMOS ocean data products. The design (see `plan.md`) introduces:

- **`constants.py`** — `Product` dataclass and five product configs (SST anomaly, marine heatwave ×2, ocean current, sea-level anomaly). Each product defines its S3 source path, variable name(s), LOD grids, chunk pixel size, and padding.
- **`services/loader.py`** — loads `xr.Dataset` from anonymous S3 (`s3fs`), time-selects for a given date, and caches results in an LRU cache keyed by `(product_id, date)`.
- **`services/renderer.py`** — converts a cached dataset into a raw PNG tile. Encoding varies by product: single-variable products use a 24-bit normalised value split across R/G/B with an ocean-mask alpha; the ocean-current product encodes U in R, V in G, ocean mask in B, A=255.
- **`routers/tiles.py`** — two endpoints:
  - `GET /tiles/{product_id}/{date}/{z}/{x}/{y}.png` — on-demand tile
  - `GET /tiles/{product_id}/{date}/manifest.json` — bounds + valueRange/uRange/vRange + LOD grid config

### Tile coordinate system

`z` = LOD level (integer, product-specific — e.g. 1/2/3 for SST), `x` = chunk column (`cx`, 0 = westernmost), `y` = chunk row (`cy`, 0 = northernmost). This is **not** Web Mercator slippy-map tiles — it is a custom atlas grid in geographic (lat/lon) space.

### PNG encoding contract

Tiles are RGBA PNGs with `optimize=False` (PIL). The byte layout is fixed and consumed by a WebGL shader:
- **24-bit scalar** (SSTA, MHW, SLA): R=high byte, G=mid byte, B=low byte of normalised uint24; A=ocean mask (255=ocean, 0=land, premultiplied).
- **Ocean current** (UV): R=U normalised to 8-bit, G=V normalised to 8-bit, B=ocean mask×255, A=255.

Normalisation ranges (`val_min`/`val_max`, `u_min`/`u_max`, etc.) are computed from the full pre-resampled dataset and stored in `manifest.json`. All tiles for a date share the same ranges — the manifest must be fetched before tiles can be decoded.
