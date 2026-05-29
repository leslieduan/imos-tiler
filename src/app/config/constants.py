from dataclasses import dataclass, field

LODIndex = int
ZoomLevel = int

# Applied by the store registry to normalise source coordinate names so the rest
# of the pipeline can assume `time` / `lat` / `lon` regardless of how a product
# names its dimensions upstream.
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


@dataclass(frozen=True)
class TileConfig:
    """Per-product tile geometry defaults — also part of the server↔shader contract.

    ``chunk_px`` is the visible tile size; ``padding`` is the extra ring of edge
    pixels included on each side so the shader can sample a bilinear filter
    without seams between tiles. Products may override either value via
    ``products.json``, but the defaults must stay in lockstep with the frontend
    atlas layout — see [[LODConfig]] for the same caveat.
    """

    chunk_px: tuple[int, int] = (240, 192)
    padding: int = 1


TILE = TileConfig()

# Bump when anything changes that would make the server render different bytes for an
# existing URL: renderer code (colormap interpolation, PNG encoder, projection algorithm,
# data normalisation), product config under the same ID, or colormap definition under the
# same name. The frontend reads this from /manifest and appends it to tile/legend URLs as
# ?cv=...; bumping it invalidates browser and CDN caches together (new URLs miss everywhere).
# Do NOT bump on every build — only on changes that affect rendered output.
# See docs/http_caching.md for the full design.
CACHE_VERSION = "cv2"
