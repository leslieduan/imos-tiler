from pydantic import BaseModel


class GeoBounds(BaseModel):
    lonMin: float
    lonMax: float
    latMin: float
    latMax: float


class LodMeta(BaseModel):
    grid: list[int]
    chunkPx: list[int]
    storedPx: list[int]
    padding: int
    zoomThreshold: int | None = None


class DataTileManifestResponse(BaseModel):
    bounds: GeoBounds
    lods: dict[str, LodMeta]
    # Scalar products populate valueRange; UV vector products populate uRange + vRange.
    # None fields are excluded from the serialized response via response_model_exclude_none.
    valueRange: list[float] | None = None
    uRange: list[float] | None = None
    vRange: list[float] | None = None
    # Categorical variables (CF flag_values) additionally populate flagValues, and
    # flagMeanings when the labels align 1:1. Absent for continuous products.
    flagValues: list[int] | None = None
    flagMeanings: list[str] | None = None
