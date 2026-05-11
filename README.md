# titiler-project

Tile server for IMOS ocean data products. Built with FastAPI, it serves map tiles for products like sea level anomaly, ocean current, and sea surface temperature.

## Setup

### Local development

```bash
# Install dependencies
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

Server available at `http://localhost:8000`. Interactive API docs at `http://localhost:8000/docs`.

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

Server is available at `http://localhost:80`.

## Endpoints

### Tiles (`/tiles`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/tiles/manifest?from=&to=` | Available dates for all products (defaults to last 3 months) |
| GET | `/tiles/{product_id}/{date}/{z}/{x}/{y}.png` | Tile image |
| GET | `/tiles/{product_id}/{date}/manifest.json` | Tile config for a product on a given date |
| GET | `/tiles/{product_id}/{date}/point?lat=&lon=` | Point value lookup |

### Admin (`/admin`)

Requires `X-Admin-Key` header. Admin endpoints are always available at port 8000 — on EC2, use an SSH tunnel (`ssh -L 8000:localhost:8000 ec2-user@your-ec2-ip`) before calling them.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/products` | List all registered products |
| POST | `/admin/products` | Register a new product |
| DELETE | `/admin/products/{id}` | Remove a product |

## Managing products

Products are stored in `products.json` and loaded on startup. Changes via the admin API take effect immediately without a restart.

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

See [`docs/security.md`](docs/security.md) for how admin endpoints are secured in production.

## Docs

- [`docs/technical.md`](docs/technical.md) — architecture, LOD algorithm, caching strategy, PNG encoding contract
- [`docs/concurrency.md`](docs/concurrency.md) — concurrency model, capacity evaluation, thread pool and cache sizing
- [`docs/dataset.md`](docs/dataset.md) — per-store variable/dimension/chunking reference
- [`docs/security.md`](docs/security.md) — admin endpoint security, API key setup, nginx, EC2 configuration
- [`docs/png-vs-webp-vs-bin.md`](docs/png-vs-webp-vs-bin.md) — tile format evaluation
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

**Branch names** must be `main` or `type/description`:
```
feat/add-sst-renderer
fix/lod-off-by-one
```
