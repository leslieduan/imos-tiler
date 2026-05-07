# Dataset Reference

Zarr stores are opened anonymously from S3. Coordinate names are normalised on open: `TIME → time`, `LATITUDE → lat`, `LONGITUDE → lon`.

---

## GSLA — Sea Level Anomaly / Ocean Current

**Store:** `s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/`
**Products:** `zarr_sea_level_anomaly`, `zarr_ocean_current`

### Dimensions

| Dimension | Size | Chunk |
|-----------|------|-------|
| TIME      | 2338 | 5     |
| LATITUDE  | 351  | 351   |
| LONGITUDE | 641  | 641   |

### Variables

| Variable | Dims                       | Dtype   | Units | Notes              |
|----------|----------------------------|---------|-------|--------------------|
| GSLA     | TIME × LATITUDE × LONGITUDE | float64 | m     | Sea level anomaly  |
| UCUR     | TIME × LATITUDE × LONGITUDE | float64 | m/s   | Eastward current   |
| VCUR     | TIME × LATITUDE × LONGITUDE | float64 | m/s   | Northward current  |
| GSL      | TIME × LATITUDE × LONGITUDE | float64 | m     | Sea level (unused) |

Chunks are full spatial slabs `(5, 351, 641)` — one S3 read fetches 5 time steps of the entire spatial grid.

---

## Radar South Australia Gulfs — Wind (Delayed QC)

**Store:** `s3://aodn-cloud-optimised/radar_SouthAustraliaGulfs_wind_delayed_qc.zarr`
**Product:** `zarr_radar_SouthAustraliaGulfs_wind_delayed_qc_wdir`

### Dimensions

| Dimension | Size  | Chunk |
|-----------|-------|-------|
| TIME      | 38129 | 100   |
| LATITUDE  | 74    | 74    |
| LONGITUDE | 102   | 102   |

Spatial coverage: South Australian Gulfs (Cape Wiles / Cape Spencer HF radar).
Data range: 2011-04-01 onwards, ~30-minute intervals.

### Variables

| Variable              | Dims                       | Dtype   | Units                          | Notes          |
|-----------------------|----------------------------|---------|--------------------------------|----------------|
| WDIR                  | TIME × LATITUDE × LONGITUDE | float64 | degrees clockwise from N       | Wind direction |
| WWAV                  | TIME × LATITUDE × LONGITUDE | float64 | degrees clockwise from N       | Wave direction |
| WWDS                  | TIME × LATITUDE × LONGITUDE | float64 | degrees                        | Wave direction spread |
| WDIR_quality_control  | TIME × LATITUDE × LONGITUDE | float64 | —                              |                |
| WWAV_quality_control  | TIME × LATITUDE × LONGITUDE | float64 | —                              |                |
| WWDS_quality_control  | TIME × LATITUDE × LONGITUDE | float64 | —                              |                |

Spatial grid is small (102 × 74) — smaller than the default `chunk_px`, so LOD auto-computes to `{1: (1, 1)}` (single tile covers the full domain).
Chunks are `(100, 74, 102)` — each S3 read fetches 100 time steps of the full spatial grid.

---

## Satellite AusTemp Heatwave 8-day

**Store:** `s3://aodn-cloud-optimised/satellite_austemp_heatwave_8day.zarr`
**Products:** `zarr_satellite_austemp_heatwave_8day_ssta`

### Dimensions

| Dimension | Size | Chunk |
|-----------|------|-------|
| time      | 5225 | 5     |
| lat       | 2000 | 1000  |
| lon       | 3900 | 1300  |

Coordinates already use lowercase (`time`, `lat`, `lon`) — no rename needed on open.
Chunks are `(5, 1000, 1300)` — spatial chunks cover half the grid per read.

### Variables

| Variable          | Dims              | Dtype   | Units           | Notes                        |
|-------------------|-------------------|---------|-----------------|------------------------------|
| ssta              | time × lat × lon  | float64 | °C              | SST anomaly                  |
| ssta_mosaic       | time × lat × lon  | float64 | °C              | SST anomaly (mosaic)         |
| sst               | time × lat × lon  | float64 | °C              | Sea surface temperature      |
| sst_mosaic        | time × lat × lon  | float64 | °C              | SST (mosaic)                 |
| dhd               | time × lat × lon  | float64 | —               | Degree heating days          |
| dhd_mosaic        | time × lat × lon  | float64 | —               | Degree heating days (mosaic) |
| dhdc              | time × lat × lon  | int32   | —               | DHD category                 |
| dhdc_mosaic       | time × lat × lon  | int32   | —               | DHD category (mosaic)        |
| MHW_category      | time × lat × lon  | float64 | —               | Marine heatwave category     |
| MHW_category_mosaic | time × lat × lon | float64 | —              | MHW category (mosaic)        |
| MCS_category      | time × lat × lon  | float64 | —               | Marine cold spell category   |
| MCS_category_mosaic | time × lat × lon | float64 | —              | MCS category (mosaic)        |
| l2p_flags         | time × lat × lon  | float64 | —               |                              |
| mosaic_age        | time × lat × lon  | int32   | —               |                              |

---

## GHRSST — Sea Surface Temperature *(data quality issue, not active)*

**Store:** `s3://aodn-cloud-optimised/satellite_ghrsst_l3s_1day_nighttime_multi_sensor_australia.zarr`

Excluded from `ZARR_PRODUCTS` because each variable has a different `TIME` dimension size (1588–1593), which prevents `xr.open_zarr` from opening the store. See the comment in `constants.py`.
