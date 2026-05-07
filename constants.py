import math
from dataclasses import dataclass, field

# Applied in loaders to normalise coordinate names across all products.
# Keys that don't exist in a dataset are silently skipped.
COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}

MAX_LODS = 4
MIN_COARSEST_GRID = (2, 2)  # minimum (cols, rows) for the coarsest LOD level

# LOD level → minimum map zoom to show that level. Applied universally to all products.
LOD_ZOOM_THRESHOLDS: dict[int, int] = {2: 4, 3: 5, 4: 6}

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

    @staticmethod
    def _compute_lod_grids(
        data_width: int,
        data_height: int,
        chunk_px: tuple[int, int],
        max_lods: int = MAX_LODS,
        min_coarsest: tuple[int, int] = MIN_COARSEST_GRID,
    ) -> dict[int, tuple[int, int]]:
        cw, ch = chunk_px
        finest_cols = max(1, math.ceil(data_width / cw))
        finest_rows = max(1, math.ceil(data_height / ch))
        max_depth = (
            math.floor(math.log2(max(finest_cols, finest_rows)))
            if max(finest_cols, finest_rows) > 1
            else 0
        )
        levels = []
        for k in range(max_depth + 1):
            scale = 2**k
            levels.append(
                (max(1, math.ceil(finest_cols / scale)), max(1, math.ceil(finest_rows / scale)))
            )
        levels.reverse()
        min_cols, min_rows = min_coarsest
        levels = [lvl for lvl in levels if lvl[0] >= min_cols and lvl[1] >= min_rows]
        return {i + 1: lvl for i, lvl in enumerate(levels[-max_lods:])}

    def apply_computed_lod_grids(self, data_width: int, data_height: int) -> None:
        """Compute and cache lod_grids from native data dimensions. No-op if already set."""
        if self.lod_grids:
            return
        object.__setattr__(
            self, "lod_grids", self._compute_lod_grids(data_width, data_height, self.chunk_px)
        )


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
    p.id: p
    for p in [
        OCEAN_CURRENT,
        SEA_LEVEL_ANOMALY,
        SST_ANOM_MOSAIC,
        MARINE_HEATWAVE_DHD_MOSAIC,
        MARINE_HEATWAVE_SSTA_MOSAIC,
    ]
}

# ── Zarr products ─────────────────────────────────────────────────────────────
_GSLA_ZARR_URL = "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/"
_SATELLITE_GHRSST_ZARR_URL = (
    "s3://aodn-cloud-optimised/satellite_ghrsst_l3s_1day_nighttime_multi_sensor_australia.zarr"
)

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

ZARR_SEA_SURFACE_TEMPERATURE = Product(
    id="zarr_sea_surface_temperature",
    source_path=_SATELLITE_GHRSST_ZARR_URL,
    variable="sea_surface_temperature",
)

ZARR_PRODUCTS: dict[str, Product] = {
    p.id: p for p in [ZARR_SEA_LEVEL_ANOMALY, ZARR_OCEAN_CURRENT, ZARR_SEA_SURFACE_TEMPERATURE]
}
