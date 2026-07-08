# CLAUDE.md

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

Non-obvious wiring:

- `routers/products.py` is `include_router`'d into both `data_tiles` and `visual_tiles`, so its handlers are exposed under both prefixes.
- Cache hierarchy in `src/app/services/caching/`: **L1** `processed_cache` (processed grids) → **L2** `slice_cache` (slices). There is no on-disk cache layer — a miss on both falls straight through to the Zarr store (`services/store/registry.py`). Backend is selectable via `CACHE_BACKEND` (`memory` default, `redis` for shared cross-instance cache on ECS, `none` to disable caching) — see `services/caching/memoizer.py`'s `CacheBackend` interface and `docs/technical.md` §10.5.
- Products and custom colormaps are static config in `src/app/config/{products,colormaps}.json`, committed with the code and loaded once on startup. There is no admin/runtime registration API — add, remove, or change one by editing the file and redeploying.

See `docs/technical.md` for full architecture, caching strategy, LOD algorithm, and PNG encoding contract.

## Critical invariants

### Date / timezone

API dates are **`TILE_TIMEZONE` local time** (default `Australia/Sydney`), not UTC. Zarr stores timestamps in UTC. Getting this wrong causes silent 404s.

- Never hardcode a timezone string — always use `LOCAL_TZ` from `src/app/utils/dates.py`
- `get_available_dates` and `load_slice` must resolve dates through the same module-level `LOCAL_TZ` — don't shadow or recompute it locally
- Clients must round-trip dates from `/manifest` — never construct them from a local clock

### LOD constants are a server↔shader contract

`LODConfig` in `src/app/config/constants.py` (`max_lods`, `min_coarsest`, `zoom_thresholds`) is baked into the frontend WebGL shader's texture atlas layout. Changing any of these without a coordinated frontend redeploy silently corrupts rendering — no error, just wrong pixels.

### Background tasks must offload heavy work

The lifespan in `src/app/main.py` schedules startup work (e.g. store prewarm) as `asyncio.create_task`s. Any CPU- or IO-heavy work inside them must go through `asyncio.to_thread`, or the event loop freezes and all in-flight requests stall.

## Testing

- **Bug fixes**: write the failing test first, then fix.
- **New endpoints / product types**: TDD the request/response contract before implementing.
- **Refactors / perf**: rely on the existing suite; don't add tests unless behavior changes.

`tests/test_invariants.py` enforces the three invariants above. If one fails, the change needs coordinated review — don't update the test to make it pass.
