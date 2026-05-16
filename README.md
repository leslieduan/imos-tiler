# titiler-project

On-demand tile server for IMOS ocean data products. Tiles are generated in real time without pre-rendering. A three-tier cache — processed grids (in-memory LRU) → slice cache (in-memory LRU) → slice files (disk) — absorbs cold S3 reads: disk-warm slices serve in ~30ms vs ~2s from S3. Products are managed at runtime via the admin API (or pre-populated in `products.json` before startup) — no redeploy required.

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

## Important: date timezone convention

> **Warning:** All dates in the API (`{date}` path params, `from`/`to` query params, `available_dates` responses) are in the server's configured local timezone. This is controlled by the `TILE_TIMEZONE` env var (default `Australia/Sydney` — AEST/AEDT). To deploy this server for a different region, set `TILE_TIMEZONE` to any valid IANA timezone name (e.g. `America/New_York`, `Europe/London`) in `.env` or `docker-compose.yml` before starting — all date conversion will use that timezone automatically.
>
> The underlying Zarr store timestamps are always UTC. The server converts between them internally.
>
> **Always use dates from the manifest — never construct them from a local clock.** Dates are opaque keys: a client constructing a date string from their own clock may produce a value that does not exist in the manifest. Passing a UTC date directly will also 404, because satellite passes typically cross midnight UTC (e.g. a Sydney daytime pass at `2022-06-01 01:20 AEST` is `2022-05-31 15:20 UTC`).
>
> See [`docs/technical.md`](docs/technical.md#9-date-timezone-and-coordinate-normalisation) for the full explanation.

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
| GET    | `/visual_tiles/colormaps/{name}/legend`                           | Color legend PNG for a colormap (gradient bar ± tick labels)                                                      |

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

Two modes are supported — see [`docs/technical.md`](docs/technical.md#83-colormap-system) for full details.

**Ramp colormap** — 2–256 evenly-spaced stops, linearly interpolated to a 256-entry LUT. Stops can be hex strings or `[r,g,b,a]` lists:

```bash
curl -X POST http://localhost:8000/admin/colormaps \
  -H "X-Admin-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "imos_sst",
    "mode": "ramp",
    "entries": ["#000080", "#00ffff", "#ffffff", "#ff8c00", "#8b0000"]
  }'
```

**Categorical colormap** — maps discrete integer data values to specific colours. `rescale=min,max` matching the key range is required at render time:

```bash
curl -X POST http://localhost:8000/admin/colormaps \
  -H "X-Admin-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "land_cover",
    "mode": "categorical",
    "entries": {"1": "#ffff00", "2": "#0000ff", "3": "#ff0000", "4": "#000000"}
  }'
```

```
# Use with rescale matching the key range
GET /visual_tiles/{product_id}/{date}/tiles/1/0/0.png?colormap=land_cover&rescale=1,4
```

**Color legend** — returns a PNG color bar for any colormap name returned by `GET /visual_tiles/colormaps`. Without `rescale`, only the bar is rendered. With `rescale=min,max`, tick labels are drawn at lo, mid, and hi:

```
GET /visual_tiles/colormaps/imos_sst/legend?rescale=-1,1&width=300&height=40&orientation=horizontal
GET /visual_tiles/colormaps/imos_sst/legend?width=40&height=256&orientation=vertical
```

**Delete a colormap:**

```bash
curl -X DELETE http://localhost:8000/admin/colormaps/imos_sst \
  -H "X-Admin-Key: your-secret-key"
```

See [`docs/security.md`](docs/security.md) for how admin endpoints are secured in production.

## Docs

- [`docs/tile_system.md`](docs/tile_system.md) — tile coordinate systems: how `data_tiles` and `visual_tiles` use `z`/`x`/`y` differently
- [`docs/technical.md`](docs/technical.md) — architecture, LOD algorithm, caching strategy, PNG encoding contract
- [`docs/cache_analysis.md`](docs/cache_analysis.md) — cache option analysis: why disk cache was chosen over Redis and EFS
- [`docs/concurrency.md`](docs/concurrency.md) — concurrency model, capacity evaluation, thread pool and cache sizing
- [`docs/dataset.md`](docs/dataset.md) — representative example Zarr stores (size classes, dimensions, chunking, variables) used as planning anchors
- [`docs/security.md`](docs/security.md) — admin endpoint security, API key setup, nginx, EC2 configuration
- [`docs/png-vs-webp-vs-bin.md`](docs/png-vs-webp-vs-bin.md) — tile format evaluation
- [`docs/benchmark.md`](docs/benchmark.md) — response time benchmarks on EC2 (cold / disk-warm / hot)
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
