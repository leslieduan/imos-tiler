from typing import Any

from pydantic import BaseModel, Field


class CoastalFillConfig(BaseModel):
    max_dist_px: int


class ProductConfig(BaseModel):
    id: str
    source_path: str
    variable: str | list[str]
    # Present in the response only when the product enables coastal fill (see
    # §7.6); omitted otherwise via response_model_exclude_none on GET /products.
    coastal_fill: CoastalFillConfig | None = None


class DateRange(BaseModel):
    # Product's full dataset bounds (earliest/latest available date), independent of
    # the from/to filter applied to `available_dates`. Both None when the product has
    # no dates at all.
    start: str | None
    end: str | None


class ProductAvailability(BaseModel):
    available_dates: list[str]
    full_date_range: DateRange


class ManifestResponse(BaseModel):
    products: dict[str, ProductAvailability]
    cache_version: str


class VariableValue(BaseModel):
    value: float | None
    units: str | None


class PointResponse(BaseModel):
    lat: float
    lon: float
    variables: dict[str, VariableValue]


class VariableInspection(BaseModel):
    dimensions: list[str]
    shape: list[int]
    dtype: str
    # Native Zarr (on-disk) chunk shape in `dimensions` order. None when the store
    # is unchunked (e.g. a contiguous array, or a numpy-backed dataset in tests).
    chunks: list[int] | None = None
    units: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class ProductInspection(BaseModel):
    id: str
    source_path: str
    dimensions: dict[str, int]
    variables: dict[str, VariableInspection]
    attributes: dict[str, Any] = Field(default_factory=dict)
