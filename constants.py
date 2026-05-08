import math
from dataclasses import dataclass, field

# Applied in loaders to normalise coordinate names across all products.
# Keys that don't exist in a dataset are silently skipped.
COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}
MAX_LODS = 4
MIN_COARSEST_GRID = (2, 2)  # minimum (cols, rows) for the coarsest LOD level
# LOD level → minimum map zoom to show that level. Applied universally to all products.
LOD_ZOOM_THRESHOLDS: dict[int, int] = {2: 4, 3: 5, 4: 6}
PADDING = 1
CHUNK_PX = (240, 192)

# dataset
_GSLA = "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/"
# satellite_ghrsst_l3s_1day_nighttime_multi_sensor_australia has data quality issue, the time dimension size is different for variables.
# _SATELLITE_GHRSST = (
#     "s3://aodn-cloud-optimised/satellite_ghrsst_l3s_1day_nighttime_multi_sensor_australia.zarr"
# )
_RADAR_ASG_WIND_DELAYED_QC = (
    "s3://aodn-cloud-optimised/radar_SouthAustraliaGulfs_wind_delayed_qc.zarr"
)
_SATELLITE_AUSTEMP_HEATWAVE_8DAY = "s3://aodn-cloud-optimised/satellite_austemp_heatwave_8day.zarr"


@dataclass(frozen=True)
class Product:
    id: str
    source_path: str
    variable: str | list[str] = ""
    lod_grids: dict[int, tuple[int, int]] = field(default_factory=dict)
    chunk_px: tuple[int, int] = CHUNK_PX
    padding: int = PADDING

    @staticmethod
    def _compute_lod_grids(
        data_width: int,
        data_height: int,
        chunk_px: tuple[int, int],
        max_lods: int = MAX_LODS,
        min_coarsest: tuple[int, int] = MIN_COARSEST_GRID,
    ) -> dict[int, tuple[int, int]]:
        # Compute how many chunks fit across the data at native resolution (finest level).
        # Then build a pyramid by halving the grid at each coarser level (doubling the scale).
        # Levels that don't meet min_coarsest are dropped; if none survive (data smaller than
        # one chunk), fall back to the native finest grid so there is always at least one LOD.
        # The finest max_lods levels are returned keyed 1..N (1 = coarsest kept).
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
        if not levels:
            levels = [(finest_cols, finest_rows)]
        return {i + 1: lvl for i, lvl in enumerate(levels[-max_lods:])}

    def apply_computed_lod_grids(self, data_width: int, data_height: int) -> None:
        """Compute and cache lod_grids from native data dimensions. No-op if already set."""
        if self.lod_grids:
            return
        object.__setattr__(
            self, "lod_grids", self._compute_lod_grids(data_width, data_height, self.chunk_px)
        )


SEA_LEVEL_ANOMALY = Product(
    id="sea_level_anomaly",
    source_path=_GSLA,
    variable="GSLA",
)
_OCEAN_CURRENT = Product(
    id="ocean_current",
    source_path=_GSLA,
    variable=["UCUR", "VCUR"],
)
# Small regional dataset: 102 lon × 74 lat — fits in a single tile (lod_grids auto-computes to {1: (1, 1)}).
_RADAR_ASG_WIND_DELAYED_QC_WDIR = Product(
    id="radar_SouthAustraliaGulfs_wind_delayed_qc_wdir",
    source_path=_RADAR_ASG_WIND_DELAYED_QC,
    variable="WDIR",
)
_SATELLITE_AUSTEMP_HEATWAVE_8DAY_SSTA = Product(
    id="satellite_austemp_heatwave_8day_ssta",
    source_path=_SATELLITE_AUSTEMP_HEATWAVE_8DAY,
    variable="ssta",
)

PRODUCTS: dict[str, Product] = {
    p.id: p
    for p in [
        SEA_LEVEL_ANOMALY,
        _OCEAN_CURRENT,
        _RADAR_ASG_WIND_DELAYED_QC_WDIR,
        _SATELLITE_AUSTEMP_HEATWAVE_8DAY_SSTA,
    ]
}
