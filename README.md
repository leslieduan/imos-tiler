# titiler-project

FastAPI tile server for IMOS ocean data products, built on [titiler-core](https://github.com/developmentseed/titiler).

Serves on-demand RGBA PNG tiles from Zarr stores on S3 in a custom geographic atlas grid (not Web Mercator). Tiles are consumed by a WebGL shader — see [PNG encoding contract](#png-encoding-contract).

## Setup

### Local development

```bash
# Install dependencies
uv sync

# Install dependencies including dev tools
uv sync --group dev

# Run the development server
uv run uvicorn main:app --reload
```

Interactive API docs available at `http://localhost:8000/docs`.

### Docker

```bash
# Build and start
docker compose up --build

# Run in background
docker compose up -d --build

# Stop
docker compose down

# Tail logs
docker compose logs -f
```

Server runs on `http://localhost:8000`.

## Endpoints

### Tiles (`/tiles`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/tiles/{product_id}/{date}/{z}/{x}/{y}.png` | RGBA tile |
| GET | `/tiles/{product_id}/{date}/manifest.json` | Bounds, value ranges, LOD grid config |
| GET | `/tiles/{product_id}/{date}/point?lat=&lon=` | Point value lookup |

**Product IDs:** `sea_level_anomaly`, `ocean_current`, `radar_SouthAustraliaGulfs_wind_delayed_qc_wdir`, `satellite_austemp_heatwave_8day_ssta`

### Tile coordinates

`z` = LOD level, `x` = chunk column (0 = westernmost), `y` = chunk row (0 = northernmost). Fetch the manifest before tiles — it contains the LOD grid dimensions and the normalisation ranges needed to decode pixel values.

## PNG encoding contract

Tiles are RGBA PNGs with `optimize=False`.

| Product type | R | G | B | A |
|---|---|---|---|---|
| Scalar (SSTA, MHW, SLA, WDIR) | high byte of uint24 | mid byte | low byte | ocean mask (255 = ocean, 0 = land) |
| Ocean current (UV) | U normalised 0–255 | V normalised 0–255 | ocean mask × 255 | 255 |

Normalisation ranges (`valueRange`, `uRange`, `vRange`) are in `manifest.json`.

## Docs

- [`docs/technical.md`](docs/technical.md) — architecture, LOD algorithm, caching strategy, PNG encoding contract
- [`docs/dataset.md`](docs/dataset.md) — per-store variable/dimension/chunking reference
- [`docs/benchmark.md`](docs/benchmark.md) — response time benchmarks (local vs EC2, cold vs hot)
- [`docs/netcdf-vs-zarr.md`](docs/netcdf-vs-zarr.md) — format comparison and IMOS product file analysis

## Development

```bash
uv run pytest          # run tests
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy .          # type check
```

### Pre-commit hooks

```bash
# Install all hook types (run once after cloning)
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push
```

**Commit messages** follow [Conventional Commits](https://www.conventionalcommits.org/):
```
feat(zarr): add manifest endpoint
fix: handle missing date in loader
```
Types: `feat` `fix` `docs` `style` `refactor` `test` `chore` `perf` `ci` `build` `revert`

**Branch names** must be `main` or `type/description`:
```
feat/add-sst-renderer
fix/lod-off-by-one
```
