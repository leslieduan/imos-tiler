from dataclasses import dataclass, field

# Applied in loaders to normalise coordinate names across all products.
# Keys that don't exist in a dataset are silently skipped.
COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}

MAX_LODS = 4
MAX_VIRTUAL_CHUNKS = 256  # grid_cols × grid_rows must not exceed this at any LOD
MIN_COARSEST_GRID = (2, 2)  # minimum (cols, rows) for the coarsest LOD level

# LOD level → minimum map zoom to show that level. Applied universally to all products.
LOD_ZOOM_THRESHOLDS: dict[int, int] = {2: 4, 3: 5,4:6}

# Fallback LOD grids for Zarr products before store dimensions are known.
DEFAULT_ZARR_LOD_GRIDS: dict[int, tuple[int, int]] = {1: (2, 2)}

# TODO: NetCDF should be deprecated from this project. auto lod_grids generate on algothrithem 
# is only enabled for ZARR. Because read Metadata for NetCDF is too heavy.


@dataclass(frozen=True)
class Product:
    id: str
    source_path: str
    variable: str | list[str] = ""
    lod_grids: dict[int, tuple[int, int]] = field(default_factory=dict)
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
)
MARINE_HEATWAVE_DHD_MOSAIC = Product(
    id="ausTemp_marine_heatwave_aus_dhd_mosaic",
    source_path="imos-data/IMOS/SRS/AusTemp/Marine-Heatwave",
    variable="dhd_mosaic",
    lod_grids={1: (3, 3), 2: (6, 5), 3: (12, 10)},
)
MARINE_HEATWAVE_SSTA_MOSAIC = Product(
    id="ausTemp_marine_heatwave_aus_ssta_mosaic",
    source_path="imos-data/IMOS/SRS/AusTemp/Marine-Heatwave",
    variable="ssta_mosaic",
    lod_grids={1: (3, 3), 2: (6, 5), 3: (12, 10)},
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
_GSLA_ZARR_URL = "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/"

ZARR_SEA_LEVEL_ANOMALY = Product(
    id="zarr_sea_level_anomaly",
    source_path=_GSLA_ZARR_URL,
    variable="GSLA",
)
ZARR_OCEAN_CURRENT = Product(
    id="zarr_ocean_current",
    source_path=_GSLA_ZARR_URL,
    variable=["UCUR", "VCUR"],
)

ZARR_PRODUCTS: dict[str, Product] = {
    p.id: p for p in [ZARR_SEA_LEVEL_ANOMALY, ZARR_OCEAN_CURRENT]
}
