# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the server (development)
uv run uvicorn app.main:app --reload

# Run tests / lint / type check
uv run pytest
uv run ruff check .
uv run mypy .
```

## Architecture

FastAPI tile server for IMOS ocean data products. Entry point is `src/app/main.py`. All tile serving goes through a Zarr-backed stack.

Two tile systems with different coordinate conventions — do not mix them up:

- **Data tiles** (`/data_tiles`) — Plate Carrée (equirectangular). `z/x/y` = LOD level / chunk col / chunk row. For WebGL shader consumption.
- **Visual tiles** (`/visual_tiles`) — Web Mercator XYZ, standard MapboxGL/Leaflet convention. Rendered via rio-tiler.

Key modules:
- `src/app/routers/public/` — public HTTP endpoints (`data_tiles.py`, `visual_tiles.py`, `products.py`); `products.py` is mounted into both `data_tiles` and `visual_tiles` via `include_router`, so every handler there is exposed under both prefixes
- `src/app/routers/admin/` — admin endpoints (auth, cache, colormaps, products)
- `src/app/services/store/` — `registry.py` (Zarr store singleton + L3 disk cache), `spatial.py` (CRS / native-resolution / default-bbox helpers)
- `src/app/services/caching/` — `slice_cache.py` (L2 in-memory slice cache), `processed_cache.py` (L1 processed grids), `disk.py`, `lifecycle.py` (prewarm + eviction)
- `src/app/services/rendering/` — `data_tiles.py` (PNG encoding for data tiles), `visual_tiles.py` (Web Mercator reprojection + colormap), `kernels.py`
- `src/app/services/product/` — product registry, manifest, dataclass
- `src/app/services/colormap/` — colormap registry, resolver, legend rendering
- `src/app/config/` — `paths.py`, `log_config.py`
- `src/app/utils/` — shared helpers: `colors.py`, `dates.py`, `geo.py`, `image.py`, `memoizer.py`

See `docs/technical.md` for full architecture, caching strategy, LOD algorithm, and PNG encoding contract.

## Critical invariants

### Date / timezone

API dates are **`TILE_TIMEZONE` local time** (default `Australia/Sydney`), not UTC. Zarr stores timestamps in UTC. Getting this wrong causes silent 404s.

- Never hardcode a timezone string — always use `LOCAL_TZ` from `src/app/utils/dates.py`
- `get_available_dates` and `load_slice` must resolve dates through the same module-level `LOCAL_TZ` — don't shadow or recompute it locally
- Clients must round-trip dates from `/manifest` — never construct them from a local clock

### LOD constants are a server↔shader contract

`LODConfig` in `src/app/constants.py` (`max_lods`, `min_coarsest`, `zoom_thresholds`) is baked into the frontend WebGL shader's texture atlas layout. Changing any of these without a coordinated frontend redeploy silently corrupts rendering — no error, just wrong pixels.

### Background tasks must offload heavy work

The lifespan in `src/app/main.py` schedules cache prewarm and refresh as `asyncio.create_task`s. Any CPU- or IO-heavy work inside them must go through `asyncio.to_thread`, or the event loop freezes and all in-flight requests stall.
