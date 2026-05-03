# titiler-project

FastAPI tile server for IMOS ocean data products, built on [titiler-core](https://github.com/developmentseed/titiler).

Serves on-demand RGBA PNG tiles from Zarr and NetCDF sources on S3 in a custom geographic atlas grid (not Web Mercator). Tiles are consumed by a WebGL shader — see [PNG encoding contract](#png-encoding-contract).

## Setup

```bash
# Install dependencies
uv sync

# Install dependencies including dev tools
uv sync --group dev

# Run the development server
uv run uvicorn main:app --reload
```

Interactive API docs available at `http://localhost:8000/docs`.

## Endpoints

### Zarr  (`/tiles/zarr`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/tiles/zarr/{product_id}/{date}/{z}/{x}/{y}.png` | RGBA tile |
| GET | `/tiles/zarr/{product_id}/{date}/manifest.json` | Bounds, value ranges, LOD grid config |
| GET | `/tiles/zarr/{product_id}/{date}/point?lat=&lon=` | Point value lookup |

**Zarr product IDs:** `zarr_sea_level_anomaly`, `zarr_ocean_current`

### NetCDF (`/tiles/netcdf`)

Same shape as Zarr. **NetCDF product IDs:** `ocean_current_gsla_ucur_vcur`, `ocean_current_gsla_gsla`, `austemp_sst_anomaly_sst_anom_mosaic`, `ausTemp_marine_heatwave_aus_dhd_mosaic`, `ausTemp_marine_heatwave_aus_ssta_mosaic`

> NetCDF support is considered legacy — Zarr is the preferred path.

### Tile coordinates

`z` = LOD level, `x` = chunk column (0 = westernmost), `y` = chunk row (0 = northernmost). Fetch the manifest before tiles — it contains the LOD grid dimensions and the normalisation ranges needed to decode pixel values.

## PNG encoding contract

Tiles are RGBA PNGs with `optimize=False`.

| Product type | R | G | B | A |
|---|---|---|---|---|
| Scalar (SSTA, MHW, SLA) | high byte of uint24 | mid byte | low byte | ocean mask (255 = ocean, 0 = land) |
| Ocean current (UV) | U normalised 0–255 | V normalised 0–255 | ocean mask × 255 | 255 |

Normalisation ranges (`valueRange`, `uRange`, `vRange`) are in `manifest.json`.

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
