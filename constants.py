from dataclasses import dataclass, field


@dataclass(frozen=True)
class Product:
    id: str
    source_path: str
    variable: str | list[str] = ""
    lod_grids: dict[int, tuple[int, int]] = field(default_factory=dict)
    lod_zoom_thresholds: dict[int, int] = field(default_factory=dict)
    chunk_px: tuple[int, int] = (240, 192)
    padding: int = 1
    # Rename map applied after open_dataset (storage-level coord names → time/lat/lon)
    coord_names: dict[str, str] = field(default_factory=dict)
    # MHW files store time as a single Int32 Unix timestamp — use isel instead of sel
    use_isel_time: bool = False


_GSLA_COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}

OCEAN_CURRENT = Product(
    id="ocean_current_gsla_ucur_vcur",
    source_path="imos-data/IMOS/OceanCurrent/GSLA/NRT",
    variable=["UCUR", "VCUR"],
    lod_grids={1: (2, 2)},
    coord_names=_GSLA_COORD_NAMES,
)
SEA_LEVEL_ANOMALY = Product(
    id="ocean_current_gsla_gsla",
    source_path="imos-data/IMOS/OceanCurrent/GSLA/NRT",
    variable="GSLA",
    lod_grids={1: (2, 2)},
    coord_names=_GSLA_COORD_NAMES,
)
SST_ANOM_MOSAIC = Product(
    id="austemp_sst_anomaly_sst_anom_mosaic",
    source_path="imos-data/IMOS/SRS/AusTemp/ssta",
    variable="sst_anom_mosaic",
    lod_grids={1: (3, 3), 2: (6, 5), 3: (12, 10)},
    lod_zoom_thresholds={2: 5, 3: 6},
)
MARINE_HEATWAVE_DHD_MOSAIC = Product(
    id="ausTemp_marine_heatwave_aus_dhd_mosaic",
    source_path="imos-data/IMOS/SRS/AusTemp/Marine-Heatwave",
    variable="dhd_mosaic",
    lod_grids={1: (3, 3), 2: (6, 5), 3: (12, 10)},
    lod_zoom_thresholds={2: 5, 3: 6},
    use_isel_time=True,
)
MARINE_HEATWAVE_SSTA_MOSAIC = Product(
    id="ausTemp_marine_heatwave_aus_ssta_mosaic",
    source_path="imos-data/IMOS/SRS/AusTemp/Marine-Heatwave",
    variable="ssta_mosaic",
    lod_grids={1: (3, 3), 2: (6, 5), 3: (12, 10)},
    lod_zoom_thresholds={2: 5, 3: 6},
    use_isel_time=True,
)

PRODUCTS: dict[str, Product] = {
    p.id: p for p in [
        OCEAN_CURRENT,
        SEA_LEVEL_ANOMALY,
        SST_ANOM_MOSAIC,
        MARINE_HEATWAVE_DHD_MOSAIC,
        MARINE_HEATWAVE_SSTA_MOSAIC,
    ]
}

# ── Zarr products ─────────────────────────────────────────────────────────────
# Single Zarr store containing all dates; coord names normalised in zarr_loader.
# source_path unused — the store URL lives in services/zarr_loader.py.
_ZARR_COORD_NAMES = {"LATITUDE": "lat", "LONGITUDE": "lon"}

ZARR_SEA_LEVEL_ANOMALY = Product(
    id="zarr_sea_level_anomaly",
    source_path="",
    variable="GSLA",
    lod_grids={1: (2, 2)},
    coord_names=_ZARR_COORD_NAMES,
)
ZARR_OCEAN_CURRENT = Product(
    id="zarr_ocean_current",
    source_path="",
    variable=["UCUR", "VCUR"],
    lod_grids={1: (2, 2)},
    coord_names=_ZARR_COORD_NAMES,
)

ZARR_PRODUCTS: dict[str, Product] = {
    p.id: p for p in [ZARR_SEA_LEVEL_ANOMALY, ZARR_OCEAN_CURRENT]
}
