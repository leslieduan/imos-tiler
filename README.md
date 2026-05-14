# titiler-project

On-demand tile server for IMOS ocean data products. Tiles are generated in real time without pre-rendering. A three-tier cache (in-memory LRU → disk → S3) keeps cold requests fast: disk-warm slices serve in ~30ms vs ~2s from S3. Products are managed at runtime via the admin API — no redeploy required.

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

First-time setup — these files must exist on the host before the first `docker compose up`:

```bash
echo "[]" > products.json && echo "{}" > colormaps.json && mkdir -p slice_cache
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

## Important: date timezone convention

> **Warning:** All dates in the API (`{date}` path params, `from`/`to` query params, `available_dates` responses) are in **local Australian time** (`Australia/Sydney` — AEST/AEDT). The underlying Zarr store timestamps are UTC. The server converts between them internally — do not bypass this by passing UTC dates directly, or tiles will 404 for any date where the satellite pass crosses midnight UTC (which is the common case for Australian daytime observations).
>
> See [`docs/technical.md`](docs/technical.md#date-and-timezone-convention) for the full explanation.

## Endpoints

### Data tiles (`/data_tiles`)

Raw RGBA tiles for WebGL shader consumption — pixel bytes encode scientific values, not colours.

| Method | Path                                                    | Description                                                  |
| ------ | ------------------------------------------------------- | ------------------------------------------------------------ |
| GET    | `/data_tiles/products`                                  | List all registered products                                 |
| GET    | `/data_tiles/manifest?from=&to=`                        | Available dates for all products (defaults to last 3 months) |
| GET    | `/data_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png` | Raw value-encoded tile                                       |
| GET    | `/data_tiles/{product_id}/{date}/manifest.json`         | Tile config (bounds, value ranges, LOD grid)                 |
| GET    | `/data_tiles/{product_id}/{date}/point?lat=&lon=`       | Point value lookup                                           |

### Visual tiles (`/visual_tiles`)

Colourised Web Mercator (XYZ) tiles — compatible with MapboxGL `raster` sources and any slippy-map library. Single-variable products only.

| Method | Path                                                              | Description                                                                                                       |
| ------ | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| GET    | `/visual_tiles/{product_id}/{date}/tiles/{z}/{x}/{y}.png`         | Colourised PNG tile (Web Mercator XYZ)                                                                            |
| GET    | `/visual_tiles/{product_id}/{date}/bbox?bbox=minx,miny,maxx,maxy` | Colourised PNG for an arbitrary bbox (EPSG:4326 degrees by default; pass `crs=EPSG:3857` for Web Mercator meters) |
| GET    | `/visual_tiles/colormaps`                                         | All supported colormap names grouped by source (custom, rio-tiler, matplotlib)                                    |

Query parameters for tile requests:

| Parameter  | Default                          | Description                                                                                         |
| ---------- | -------------------------------- | --------------------------------------------------------------------------------------------------- |
| `colormap` | `viridis`                        | Colormap name — any matplotlib or rio-tiler built-in, or a custom name registered via the admin API |
| `rescale`  | auto (data min/max for the date) | Value range as `min,max`, e.g. `-0.5,0.5`                                                           |

### Admin (`/admin`)

Requires `X-Admin-Key` header. Admin endpoints are blocked at the nginx layer and only reachable at port 8000 — on EC2, use an SSH tunnel (`ssh -L 8000:localhost:8000 ec2-user@your-ec2-ip`) before calling them.

| Method | Path                      | Description                    |
| ------ | ------------------------- | ------------------------------ |
| POST   | `/admin/products`         | Register a new product         |
| DELETE | `/admin/products/{id}`    | Remove a product               |
| POST   | `/admin/colormaps`        | Register a new custom colormap |
| DELETE | `/admin/colormaps/{name}` | Remove a custom colormap       |

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

## Managing colormaps

Custom colormaps are stored in `colormaps.json` and loaded on startup. Changes via the admin API take effect immediately without a restart. All supported colormap names (custom, rio-tiler built-ins, and matplotlib) can be browsed via `GET /visual_tiles/colormaps`. Names registered here can be used via `?colormap=<name>` on any visual tile request.

Each colormap is exactly 256 RGBA entries (one per normalised byte value 0–255).

**Add a colormap:**

```bash
curl -X POST http://localhost:8000/admin/colormaps \
  -H "X-Admin-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "imos_sst",
    "entries": [[0,0,80,255], [0,40,160,255], ..., [220,240,255,255]]
  }'
```

**Delete a colormap:**

```bash
curl -X DELETE http://localhost:8000/admin/colormaps/imos_sst \
  -H "X-Admin-Key: your-secret-key"
```

Compile-time defaults can also be added directly in `CUSTOM_COLORMAPS` in `constants.py` — these are always available regardless of `colormaps.json`.

See [`docs/security.md`](docs/security.md) for how admin endpoints are secured in production.

## Docs

- [`docs/tile_system.md`](docs/tile_system.md) — tile coordinate systems: how `data_tiles` and `visual_tiles` use `z`/`x`/`y` differently
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
