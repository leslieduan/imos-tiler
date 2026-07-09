# imos-tiler

On-demand tile server for IMOS ocean data products. **Scope: gridded data stored as Zarr on S3 only**.  Tiles are generated in real time without pre-rendering. A two-tier in-memory cache — processed grids (LRU) → slice cache (LRU) — absorbs cold S3 reads. Products and custom colormaps are static config in `src/app/config/{products,colormaps}.json`, committed with the code — add, remove, or change one by editing the file and redeploying.

## Setup

### Local development

```bash
# Install dependencies
uv sync --group dev
```

```bash
# Run the development server
uv run uvicorn app.main:app --reload
```

All server configuration (timezone, cache backend, S3 timeouts, log level, etc.) lives in [`src/app/config/settings.py`](src/app/config/settings.py) as plain constants — no env vars, no `.env` file. To change a value, edit the file and restart the server.

To enable debug-level application logs (e.g. to see sub-daily timestamp collisions or cache internals), set `LOG_LEVEL = "DEBUG"` in `settings.py`.

Server available at `http://localhost:8000`. Interactive API docs at `http://localhost:8000/docs`.

## Important: date timezone convention

> **Warning:** All dates in the API (`{date}` path params, `from`/`to` query params, `available_dates` responses) are in the server's configured local timezone. This is controlled by the `TILE_TIMEZONE` constant in `src/app/config/settings.py` (default `Australia/Sydney` — AEST/AEDT). To deploy this server for a different region, set `TILE_TIMEZONE` to any valid IANA timezone name (e.g. `America/New_York`, `Europe/London`) before starting — all date conversion will use that timezone automatically.
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

### Visual tiles (`/visual_tiles`)

Colourised Web Mercator (XYZ) tiles — compatible with MapboxGL `raster` sources and any slippy-map library. Single-variable products only.

| Method | Path                                                              | Description                                                                                                       |
| ------ | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| GET    | `/visual_tiles/{product_id}/{date}/{z}/{x}/{y}.{ext}`               | Colourised tile (Web Mercator XYZ). `ext` is `png` (lossless) or `webp` (lossy, ~50% smaller). Categorical colormaps must use `.png`. |
| GET    | `/visual_tiles/{product_id}/{date}/bbox.{ext}?bbox=minx,miny,maxx,maxy` | Colourised image for an arbitrary bbox (EPSG:4326 degrees by default; pass `crs=EPSG:3857` for Web Mercator meters). `ext` is `png` or `webp`. |
| GET    | `/visual_tiles/{product_id}/{from_date}/{to_date}/animation.{ext}` | Animated bbox over a date range. `ext` is `gif`, `apng`, or `webp`. Bbox defaults to dataset extent; width/height default to native cell count or are derived from bbox aspect ratio. 30-frame cap. Intended for demos — see [technical doc §6.3.1](docs/technical.md#631-animation-endpoint) for the caching trade-offs. |
| GET    | `/visual_tiles/colormaps`                                           | All supported colormap names grouped by source (custom, rio-tiler, matplotlib)                                    |
| GET    | `/visual_tiles/colormaps/{name}/legend`                             | Color legend PNG for a colormap (gradient bar ± tick labels)                                                      |

> The product-metadata endpoints `/products`, `/manifest`, and `/{product_id}/{date}/point` listed under **Data tiles** above are also served under `/visual_tiles/…` with identical behaviour — the same router backs both prefixes — so a visual-only client never needs to call `/data_tiles`.

Query parameters for tile requests:

| Parameter  | Default                          | Description                                                                                         |
| ---------- | -------------------------------- | --------------------------------------------------------------------------------------------------- |
| `colormap` | `viridis`                        | Colormap name — any matplotlib or rio-tiler built-in, or a custom name defined in `config/colormaps.json` |
| `rescale`  | auto (data min/max for the date) | Value range as `min,max`, e.g. `-0.5,0.5`                                                           |

## Managing products

Products are static config in [`src/app/config/products.json`](src/app/config/products.json), loaded once on startup. There is no runtime registration API — add, remove, or change a product by editing the file and redeploying.

**Add a product** — append an entry to `src/app/config/products.json` and redeploy:

```json
{
  "id": "sea_level_anomaly",
  "source_path": "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/",
  "variable": "GSLA"
}
```

**Add a product with multiple variables (e.g. UV current):**

```json
{
  "id": "ocean_current",
  "source_path": "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/",
  "variable": ["UCUR", "VCUR"]
}
```

**Remove a product** — delete its entry from the file and redeploy.

## Managing colormaps

Custom colormaps are static config in [`src/app/config/colormaps.json`](src/app/config/colormaps.json), loaded once on startup. All supported colormap names (custom, rio-tiler built-ins, and matplotlib) can be browsed via `GET /visual_tiles/colormaps`. Names defined here can be used via `?colormap=<name>` on any visual tile request.

Two modes are supported — see [`docs/technical.md`](docs/technical.md#83-colormap-system) for full details. Entries in the file are already expanded to a 256-entry LUT; use `app/utils/colors.py`'s `interpolate_colormap` / `build_categorical_lut` to build a new entry from a short list of stops before adding it.

**Ramp colormap** — 2–256 evenly-spaced stops, linearly interpolated to a 256-entry LUT. Stops can be hex strings or `[r,g,b,a]` lists:

```json
{
  "name": "imos_sst",
  "mode": "ramp",
  "entries": ["#000080", "#00ffff", "#ffffff", "#ff8c00", "#8b0000"]
}
```

**Categorical colormap** — maps discrete integer data values to specific colours. `rescale=min,max` matching the key range is required at render time:

```json
{
  "name": "land_cover",
  "mode": "categorical",
  "entries": {"1": "#ffff00", "2": "#0000ff", "3": "#ff0000", "4": "#000000"}
}
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

**Remove a colormap** — delete its entry from the file and redeploy.

## Docs

- [`docs/technical.md`](docs/technical.md) — architecture, tile coordinate systems, LOD algorithm, caching strategy, concurrency model, capacity planning, PNG encoding contract, logging
- [`docs/http_caching.md`](docs/http_caching.md) — HTTP caching design: Cache-Control headers, ETag revalidation on `/manifest`, CACHE_VERSION invalidation

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
