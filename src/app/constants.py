import math
from dataclasses import dataclass, field

LODIndex = int
ZoomLevel = int

# Applied in loaders to normalise coordinate names across all products.
COORD_NAMES = {"TIME": "time", "LATITUDE": "lat", "LONGITUDE": "lon"}


@dataclass(frozen=True)
class LODConfig:
    """Server-shader contract for the data-tile LOD pyramid.

    Bundled here (rather than passed at runtime or read from env) because these
    values are baked into the WebGL shader on the frontend — changing one without
    redeploying the frontend silently corrupts the rendering.
    """

    # Cap on LOD levels per product. The frontend packs all LODs into a single WebGL
    # texture atlas hard-capped at 4096×4096 (~64 MB VRAM per atlas) regardless of
    # gl.MAX_TEXTURE_SIZE. Going above 4 doesn't break rendering — the atlas falls
    # back to LRU eviction — but causes visible tile re-upload churn as the user
    # pans/zooms. 4 is the value tuned to fit comfortably under the cap.
    max_lods: int = 4
    # Minimum (cols, rows) for the coarsest level; levels below this are dropped.
    min_coarsest: tuple[int, int] = (2, 2)
    # LOD level → minimum map zoom to show that level. Applied universally to all products.
    # LOD1 is the coarsest level, so under zoom level 4 only LOD1 tiles are shown; at zoom 4 LOD2
    # tiles are shown, etc.
    zoom_thresholds: dict[LODIndex, ZoomLevel] = field(default_factory=lambda: {2: 4, 3: 5, 4: 6})


LOD = LODConfig()

PADDING = 1
CHUNK_PX = (240, 192)

PRODUCTS_CONFIG_PATH = "data/products.json"
COLORMAPS_CONFIG_PATH = "data/colormaps.json"
DISK_CACHE_PATH = "slice_cache"

# Bump when anything changes that would make the server render different bytes for an
# existing URL: renderer code (colormap interpolation, PNG encoder, projection algorithm,
# data normalisation), product config under the same ID, or colormap definition under the
# same name. The frontend reads this from /manifest and appends it to tile/legend URLs as
# ?cv=...; bumping it invalidates browser and CDN caches together (new URLs miss everywhere).
# Do NOT bump on every build — only on changes that affect rendered output.
# See docs/http_caching.md for the full design.
CACHE_VERSION = "cv1"


@dataclass(frozen=True)
class Product:
    id: str
    source_path: str
    variable: str | list[str]
    lod_grids: dict[int, tuple[int, int]] = field(default_factory=dict)
    chunk_px: tuple[int, int] = CHUNK_PX
    padding: int = PADDING

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


PRODUCTS: dict[str, Product] = {}
