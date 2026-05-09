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
```

Create a `.env` file in the project root (never commit this):
```bash
ADMIN_API_KEY=your-secret-key
```

```bash
# Run the development server (.env is loaded automatically)
uv run uvicorn main:app --reload
```

Interactive API docs available at `http://localhost:8000/docs`.

### Docker

Create a `.env` file in the project root before starting:
```bash
ADMIN_API_KEY=your-secret-key
```

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

Public tile server runs on `http://localhost:80` via nginx. Port 8000 is also accessible locally (not published to the internet on EC2).

## Endpoints

### Tiles (`/tiles`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/tiles/{product_id}/{date}/{z}/{x}/{y}.png` | RGBA tile |
| GET | `/tiles/{product_id}/{date}/manifest.json` | Bounds, value ranges, LOD grid config |
| GET | `/tiles/{product_id}/{date}/point?lat=&lon=` | Point value lookup |

`z` = LOD level, `x` = chunk column (0 = westernmost), `y` = chunk row (0 = northernmost). Fetch the manifest before tiles — it contains the LOD grid dimensions and the normalisation ranges needed to decode pixel values.

### Admin (`/admin`)

Requires `X-Admin-Key` header. How to reach these endpoints depends on where the server is running:

| Environment | How to call admin endpoints |
|-------------|----------------------------|
| Local (`uv run`) | `http://localhost:8000/admin/...` directly |
| Local (Docker) | `http://localhost:8000/admin/...` directly — port 8000 is accessible on your machine even though nginx blocks it on port 80 |
| EC2 (Docker) | SSH tunnel first: `ssh -L 8000:localhost:8000 ec2-user@your-ec2-ip`, then `http://localhost:8000/admin/...` |

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/products` | List all registered products |
| POST | `/admin/products` | Register a new product |
| DELETE | `/admin/products/{id}` | Remove a product |

## Managing products

Products are stored in `products.json` (the single source of truth). On startup the server reads this file into memory. Changes via the admin API are written to `products.json` immediately and take effect without a restart.

**Add a product:**
```bash
curl -X POST http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "sea_level_anomaly",
    "source_path": "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/",
    "variable": "GSLA"
  }'
```

**Add a product with multiple variables (e.g. UV current):**
```bash
curl -X POST http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "ocean_current",
    "source_path": "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/",
    "variable": ["UCUR", "VCUR"]
  }'
```

**Delete a product:**
```bash
curl -X DELETE http://localhost:8000/admin/products/sea_level_anomaly \
  -H "X-Admin-Key: your-secret-key"
```

The store must have `lat`/`lon` dimensions (or the uppercase variants `LATITUDE`/`LONGITUDE`, which are renamed automatically). LOD grids, rendering, and manifest generation are all derived automatically from the store's dimensions and the variable name.

See [`docs/security.md`](docs/security.md) for how admin endpoints are secured in production.

## PNG encoding contract

Tiles are RGBA PNGs with `optimize=False`.

| Product type | R | G | B | A |
|---|---|---|---|---|
| Scalar (SSTA, MHW, SLA, WDIR) | high byte of uint24 | mid byte | low byte | ocean mask (255 = ocean, 0 = land) |
| Particle / vector (UV — e.g. ocean current, wind) | U normalised 0–255 | V normalised 0–255 | ocean mask × 255 | 255 |

Normalisation ranges (`valueRange`, `uRange`, `vRange`) are in `manifest.json`.

## Docs

- [`docs/technical.md`](docs/technical.md) — architecture, LOD algorithm, caching strategy, PNG encoding contract
- [`docs/dataset.md`](docs/dataset.md) — per-store variable/dimension/chunking reference
- [`docs/security.md`](docs/security.md) — admin endpoint security, API key setup, nginx, EC2 configuration
- [`docs/png-vs-webp-vs-bin.md`](docs/png-vs-webp-vs-bin.md) — tile format evaluation: why PNG is used over WebP or raw binary
- [`docs/titiler-xarray-evaluation.md`](docs/titiler-xarray-evaluation.md) — why titiler.xarray is not used in this project
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
