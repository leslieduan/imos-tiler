import math
from dataclasses import dataclass, field

from app.constants import LOD, TILE


@dataclass(frozen=True)
class Product:
    id: str
    source_path: str
    variable: str | list[str]
    lod_grids: dict[int, tuple[int, int]] = field(default_factory=dict)
    chunk_px: tuple[int, int] = TILE.chunk_px
    padding: int = TILE.padding

    def __post_init__(self) -> None:
        if not self.variable:
            raise ValueError(f"Product '{self.id}' must specify at least one variable")

    @staticmethod
    def _compute_lod_grids(
        data_width: int,
        data_height: int,
        chunk_px: tuple[int, int],
        max_lods: int = LOD.max_lods,
        min_coarsest: tuple[int, int] = LOD.min_coarsest,
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

    @property
    def variables(self) -> list[str]:
        return self.variable if isinstance(self.variable, list) else [self.variable]

    def apply_computed_lod_grids(self, data_width: int, data_height: int) -> None:
        """Compute and cache lod_grids from native data dimensions. No-op if already set."""
        if self.lod_grids:
            return
        self.lod_grids.update(self._compute_lod_grids(data_width, data_height, self.chunk_px))
