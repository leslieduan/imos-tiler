from dataclasses import dataclass, field

# Applied in loaders to normalise coordinate names across all products.
# Keys that don't exist in a dataset are silently skipped.
COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}


@dataclass(frozen=True)
class Product:
    id: str
    source_path: str
    variable: str | list[str] = ""
    lod_grids: dict[int, tuple[int, int]] = field(default_factory=dict)
    lod_zoom_thresholds: dict[int, int] = field(default_factory=dict)
    chunk_px: tuple[int, int] = (240, 192)
    padding: int = 1


OCEAN_CURRENT = Product(
    id="ocean_current_gsla_ucur_vcur",
    source_path="imos-data/IMOS/OceanCurrent/GSLA/NRT",
    variable=["UCUR", "VCUR"],
    lod_grids={1: (2, 2)},
)
SEA_LEVEL_ANOMALY = Product(
    id="ocean_current_gsla_gsla",
    source_path="imos-data/IMOS/OceanCurrent/GSLA/NRT",
    variable="GSLA",
    lod_grids={1: (2, 2)},
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
)
MARINE_HEATWAVE_SSTA_MOSAIC = Product(
    id="ausTemp_marine_heatwave_aus_ssta_mosaic",
    source_path="imos-data/IMOS/SRS/AusTemp/Marine-Heatwave",
    variable="ssta_mosaic",
    lod_grids={1: (3, 3), 2: (6, 5), 3: (12, 10)},
    lod_zoom_thresholds={2: 5, 3: 6},
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
# Single Zarr store containing all dates; source_path unused.
ZARR_SEA_LEVEL_ANOMALY = Product(
    id="zarr_sea_level_anomaly",
    source_path="",
    variable="GSLA",
    lod_grids={1: (2, 2)},
)
ZARR_OCEAN_CURRENT = Product(
    id="zarr_ocean_current",
    source_path="",
    variable=["UCUR", "VCUR"],
    lod_grids={1: (2, 2)},
)

ZARR_PRODUCTS: dict[str, Product] = {
    p.id: p for p in [ZARR_SEA_LEVEL_ANOMALY, ZARR_OCEAN_CURRENT]
}
