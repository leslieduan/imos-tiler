# imos-tiler

On-demand tile server for IMOS ocean data products. **Scope: gridded data stored as Zarr on S3 only**.  Tiles are generated in real time without pre-rendering. A three-tier cache — processed grids (in-memory LRU) → slice cache (in-memory LRU) → slice files (disk) — absorbs cold S3 reads: disk-warm slices serve in ~30ms vs ~2s from S3. Products are managed at runtime via the admin API (or pre-populated in `products.json` before startup) — no redeploy required.

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
uv run uvicorn app.main:app --reload
```

To enable debug-level application logs (e.g. to see sub-daily timestamp collisions or cache internals):

```bash
LOG_LEVEL=DEBUG uv run uvicorn app.main:app --reload
```

`LOG_LEVEL` accepts any standard Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Default is `INFO`.

Server available at `http://localhost:8000`. Interactive API docs at `http://localhost:8000/docs`.

### Docker

Create a `.env` file in the project root before starting:

```bash
ADMIN_API_KEY=your-secret-key
```

To enable debug logs, add `LOG_LEVEL=DEBUG` to `.env` (logs go to CloudWatch in JSON format):

```bash
ADMIN_API_KEY=your-secret-key
LOG_LEVEL=DEBUG
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
| GET    | `/data_tiles/manifest?from=&to=`                        | Available dates + full date range per product (`from` defaults to each product's earliest date) |
| GET    | `/data_tiles/{product_id}/{date}/{z}/{x}/{y}.png`       | Raw value-encoded tile                                       |
| GET    | `/data_tiles/{product_id}/{date}/manifest.json`         | Tile config (bounds, value ranges, LOD grid)                 |
| GET    | `/data_tiles/{product_id}/{date}/point?lat=&lon=`       | Point value lookup (single date)                             |
| GET    | `/data_tiles/{product_id}/timeseries?lat=&lon=&from=&to=` | Point value per date over a range. Slow for long ranges — see [timeseries performance](docs/timeseries_performance.md) |

### Visual tiles (`/visual_tiles`)

Colourised Web Mercator (XYZ) tiles — compatible with MapboxGL `raster` sources and any slippy-map library. Single-variable products only.

| Method | Path                                                              | Description                                                                                                       |
| ------ | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| GET    | `/visual_tiles/{product_id}/{date}/{z}/{x}/{y}.{ext}`               | Colourised tile (Web Mercator XYZ). `ext` is `png` (lossless) or `webp` (lossy, ~50% smaller). Categorical colormaps must use `.png`. |
| GET    | `/visual_tiles/{product_id}/{date}/bbox.{ext}?bbox=minx,miny,maxx,maxy` | Colourised image for an arbitrary bbox (EPSG:4326 degrees by default; pass `crs=EPSG:3857` for Web Mercator meters). `ext` is `png` or `webp`. |
| GET    | `/visual_tiles/{product_id}/{from_date}/{to_date}/animation.{ext}` | Animated bbox over a date range. `ext` is `gif`, `apng`, or `webp`. Bbox defaults to dataset extent; width/height default to native cell count or are derived from bbox aspect ratio. 30-frame cap. Intended for demos — see [technical doc §6.3.1](docs/technical.md#631-animation-endpoint) for the caching trade-offs. |
| GET    | `/visual_tiles/colormaps`                                           | All supported colormap names grouped by source (custom, rio-tiler, matplotlib)                                    |
| GET    | `/visual_tiles/colormaps/{name}/legend`                             | Color legend PNG for a colormap (gradient bar ± tick labels)                                                      |

> The product-metadata endpoints `/products`, `/manifest`, `/{product_id}/{date}/point`, and `/{product_id}/timeseries` listed under **Data tiles** above are also served under `/visual_tiles/…` with identical behaviour — the same router backs both prefixes — so a visual-only client never needs to call `/data_tiles`.

Query parameters for tile requests:

| Parameter  | Default                          | Description                                                                                         |
| ---------- | -------------------------------- | --------------------------------------------------------------------------------------------------- |
| `colormap` | `viridis`                        | Colormap name — any matplotlib or rio-tiler built-in, or a custom name registered via the admin API |
| `rescale`  | auto (data min/max for the date) | Value range as `min,max`, e.g. `-0.5,0.5`                                                           |

### Admin (`/admin`)

Requires `X-Admin-Key` header. Admin endpoints are blocked at the nginx layer and only reachable at port 8000 — on EC2, use an SSH tunnel (`ssh -L 8000:localhost:8000 ec2-user@your-ec2-ip`) before calling them.

| Method | Path                      | Description                                                                |
| ------ | ------------------------- | -------------------------------------------------------------------------- |
| POST   | `/admin/products`         | Register a new product                                                     |
| DELETE | `/admin/products/{id}`    | Remove a product                                                           |
| POST   | `/admin/colormaps`        | Register a new custom colormap                                             |
| DELETE | `/admin/colormaps/{name}` | Remove a custom colormap                                                   |
| GET    | `/admin/cache`            | Cache state snapshot (disk footprint, refresh status, in-flight computes) |
| DELETE | `/admin/cache/memory`     | Clear all in-memory caches (L1 processed grids + L2 slices). Disk untouched. |
| DELETE | `/admin/cache/disk`       | Delete every slice file from the L3 disk cache. Memory caches untouched. |

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
GET /visual_tiles/{product_id}/{date}/1/0/0.png?colormap=land_cover&rescale=1,4
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

- [`docs/technical.md`](docs/technical.md) — architecture, tile coordinate systems, LOD algorithm, caching strategy, concurrency model, capacity planning, PNG encoding contract, logging
- [`docs/cache_analysis.md`](docs/cache_analysis.md) — cache option analysis: why disk cache was chosen over Redis and EFS
- [`docs/http_caching.md`](docs/http_caching.md) — HTTP caching design: Cache-Control headers, ETag revalidation on `/manifest`, CACHE_VERSION invalidation
- [`docs/timeseries_performance.md`](docs/timeseries_performance.md) — why the `/timeseries` (pixel-drill) endpoint is slow for long ranges, and what to do about it
- [`docs/dataset.md`](docs/dataset.md) — representative example Zarr stores (size classes, dimensions, chunking, variables) used as planning anchors
- [`docs/security.md`](docs/security.md) — admin endpoint security, API key setup, nginx, EC2 configuration
- [`docs/png-vs-webp-vs-bin.md`](docs/png-vs-webp-vs-bin.md) — tile format evaluation
- [`docs/benchmark.md`](docs/benchmark.md) — response time benchmarks on EC2 (cold / disk-warm / hot)
- [`docs/netcdf-vs-zarr.md`](docs/netcdf-vs-zarr.md) — format comparison and IMOS product file analysis

## Development

```bash
uv run pytest                       # run tests
uv run pytest --cov                 # run tests with coverage report (whole project)
uv run pytest --cov=src/app         # run tests with coverage scoped to app/
uv run ruff check .                 # lint
uv run ruff check . --fix           # lint and auto-fix
uv run ruff format .                # format (writes changes in place)
uv run ruff format --check .        # verify formatting only (what CI runs)
uv run mypy .                       # type check (whole project, including scripts/)
```

> `ruff format .` rewrites files in place. Use `--check` in scripts/hooks when you only want to verify.

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
