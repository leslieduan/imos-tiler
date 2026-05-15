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

FastAPI tile server for IMOS ocean data products. Entry point is `main.py`. All tile serving goes through a Zarr-backed stack.

Two tile systems with different coordinate conventions — do not mix them up:

- **Data tiles** (`/data_tiles`) — Plate Carrée (equirectangular). `z/x/y` = LOD level / chunk col / chunk row. For WebGL shader consumption.
- **Visual tiles** (`/visual_tiles`) — Web Mercator XYZ, standard MapboxGL/Leaflet convention. Rendered via rio-tiler.

Key modules:
- `routers/` — HTTP endpoints; `products.py` is shared between both tile routers
- `services/loader.py` — Zarr store singleton, L2 in-memory slice cache, L3 disk cache
- `services/data_renderer.py` — L1 processed grid cache, PNG encoding (data tiles)
- `services/visual_renderer.py` — Web Mercator reprojection, colormap lookup (visual tiles)
- `services/product_store.py` / `colormap_store.py` — runtime config persistence
- `utils/` — shared helpers: `colors.py`, `dates.py`, `geo.py`

See `docs/technical.md` for full architecture, caching strategy, LOD algorithm, and PNG encoding contract.

## Critical invariants

### Date / timezone

API dates are **`TILE_TIMEZONE` local time** (default `Australia/Sydney`), not UTC. Zarr stores timestamps in UTC. Getting this wrong causes silent 404s.

- Never hardcode a timezone string — always use `_LOCAL_TZ` from `services/loader.py`
- `get_available_dates` and `load_slice` must always use the same `_LOCAL_TZ` value
- Clients must round-trip dates from `/manifest` — never construct them from a local clock

### Admin API

All `/admin` endpoints require the `X-Admin-Key` request header. Key is set via the `ADMIN_KEY` environment variable.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TILE_TIMEZONE` | `Australia/Sydney` | IANA timezone for date conversion |
| `ADMIN_KEY` | _(required)_ | Secret key for `/admin` endpoints |
| `DISK_CACHE_PATH` | _(unset)_ | Absolute path for L3 disk cache; disabled if unset |
| `CACHE_DAYS` | `30` | How many recent dates to prewarm per product |
