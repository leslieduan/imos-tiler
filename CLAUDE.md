# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the server (development)
uv run uvicorn main:app --reload

# Run tests / lint / type check
uv run pytest
uv run ruff check .
uv run mypy .
```

## Architecture

FastAPI tile server for IMOS ocean data products. Entry point is `main.py`. All tile serving goes through a Zarr-backed stack.

Two tile systems with different coordinate conventions — do not mix them up:

- **Data tiles** (`/data_tiles`) — Plate Carrée (equirectangular). `z/x/y` = LOD level / chunk col / chunk row. For WebGL shader consumption.
- **Visual tiles** (`/visual_tiles`) — Web Mercator XYZ, standard MapboxGL/Leaflet convention. Rendered via rio-tiler.

Key modules:
- `routers/` — HTTP endpoints; `products.py` is mounted into both `data_tiles` and `visual_tiles` via `include_router`, so every handler there is exposed under both prefixes
- `services/loader.py` — Zarr store singleton, L2 in-memory slice cache, L3 disk cache
- `services/data_renderer.py` — L1 processed grid cache, PNG encoding (data tiles)
- `services/visual_renderer.py` — Web Mercator reprojection, colormap lookup (visual tiles)
- `services/product_store.py` / `colormap_store.py` — runtime config persistence
- `utils/` — shared helpers: `colors.py`, `dates.py`, `geo.py`

See `docs/technical.md` for full architecture, caching strategy, LOD algorithm, and PNG encoding contract.

## Critical invariants

### Date / timezone

API dates are **`TILE_TIMEZONE` local time** (default `Australia/Sydney`), not UTC. Zarr stores timestamps in UTC. Getting this wrong causes silent 404s.

- Never hardcode a timezone string — always use `LOCAL_TZ` from `utils/dates.py`
- `get_available_dates` and `load_slice` must resolve dates through the same module-level `LOCAL_TZ` — don't shadow or recompute it locally
- Clients must round-trip dates from `/manifest` — never construct them from a local clock

### LOD constants are a server↔shader contract

`LODConfig` in `constants.py` (`max_lods`, `min_coarsest`, `zoom_thresholds`) is baked into the frontend WebGL shader's texture atlas layout. Changing any of these without a coordinated frontend redeploy silently corrupts rendering — no error, just wrong pixels.

### Background tasks must offload heavy work

The lifespan in `main.py` schedules cache prewarm and refresh as `asyncio.create_task`s. Any CPU- or IO-heavy work inside them must go through `asyncio.to_thread`, or the event loop freezes and all in-flight requests stall.
