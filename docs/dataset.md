# Dataset Reference

> **Scope.** This document describes a handful of IMOS Zarr stores that have been used in development as concrete examples of the kind of data this server consumes. **It is not an exhaustive list of products served by any given deployment.** Products are registered at runtime (or pre-populated in `products.json` at bootstrap) via the admin API — see [`technical.md` §13](technical.md#13-adding-a-new-product). A production deployment will typically register many more satellite-class products than are listed here, and the registry is expected to grow over time without touching this file.
>
> The purpose of this reference is to help engineers reason about the **shape, dimensions, chunking, and variables** of the kinds of Zarr stores the server supports, so they can confirm a new candidate store will work with the existing pipelines (see the Zarr store requirements in [`technical.md` §13.3](technical.md#133-requirements-for-the-zarr-store)).

Zarr stores are opened anonymously from S3. Coordinate names are normalised on open: `TIME → time`, `LATITUDE → lat`, `LONGITUDE → lon`.

---

## GSLA — Sea Level Anomaly / Ocean Current

**Store:** `s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/`
**Example product IDs:** `sea_level_anomaly`, `ocean_current`

### Dimensions

| Dimension | Size | Chunk |
| --------- | ---- | ----- |
| TIME      | 2338 | 5     |
| LATITUDE  | 351  | 351   |
| LONGITUDE | 641  | 641   |

### Variables

| Variable | Dims                        | Dtype   | Units | Notes              |
| -------- | --------------------------- | ------- | ----- | ------------------ |
| GSLA     | TIME × LATITUDE × LONGITUDE | float64 | m     | Sea level anomaly  |
| UCUR     | TIME × LATITUDE × LONGITUDE | float64 | m/s   | Eastward current   |
| VCUR     | TIME × LATITUDE × LONGITUDE | float64 | m/s   | Northward current  |
| GSL      | TIME × LATITUDE × LONGITUDE | float64 | m     | Sea level (unused) |

Chunks are full spatial slabs `(5, 351, 641)` — one S3 read fetches 5 time steps of the entire spatial grid.

---

## Satellite AusTemp Heatwave 8-day

**Store:** `s3://aodn-cloud-optimised/satellite_austemp_heatwave_8day.zarr`
**Example product ID:** `satellite_austemp_heatwave_8day_ssta`

This store is the canonical example of a **satellite-class** product — large grid, many variables, dominant contributor to RAM and disk usage in production. The capacity-planning numbers in [`technical.md` §14](technical.md#14-capacity-and-resource-planning) are calibrated against this shape (2000 × 3900 float64 → ~61 MB raw / ~18 MB lz4 per date).

### Dimensions

| Dimension | Size | Chunk |
| --------- | ---- | ----- |
| time      | 5225 | 5     |
| lat       | 2000 | 1000  |
| lon       | 3900 | 1300  |

Coordinates already use lowercase (`time`, `lat`, `lon`) — no rename needed on open.
Chunks are `(5, 1000, 1300)` — spatial chunks cover half the grid per read.

### Variables

| Variable            | Dims             | Dtype   | Units | Notes                        |
| ------------------- | ---------------- | ------- | ----- | ---------------------------- |
| ssta                | time × lat × lon | float64 | °C    | SST anomaly                  |
| ssta_mosaic         | time × lat × lon | float64 | °C    | SST anomaly (mosaic)         |
| sst                 | time × lat × lon | float64 | °C    | Sea surface temperature      |
| sst_mosaic          | time × lat × lon | float64 | °C    | SST (mosaic)                 |
| dhd                 | time × lat × lon | float64 | —     | Degree heating days          |
| dhd_mosaic          | time × lat × lon | float64 | —     | Degree heating days (mosaic) |
| dhdc                | time × lat × lon | int32   | —     | DHD category                 |
| dhdc_mosaic         | time × lat × lon | int32   | —     | DHD category (mosaic)        |
| MHW_category        | time × lat × lon | float64 | —     | Marine heatwave category     |
| MHW_category_mosaic | time × lat × lon | float64 | —     | MHW category (mosaic)        |
| MCS_category        | time × lat × lon | float64 | —     | Marine cold spell category   |
| MCS_category_mosaic | time × lat × lon | float64 | —     | MCS category (mosaic)        |
| l2p_flags           | time × lat × lon | float64 | —     |                              |
| mosaic_age          | time × lat × lon | int32   | —     |                              |
