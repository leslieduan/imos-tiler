from pydantic import BaseModel


class ProductConfig(BaseModel):
    id: str
    source_path: str
    variable: str | list[str]
    chunk_px: list[int]
    padding: int


class ProductAvailability(BaseModel):
    available_dates: list[str]


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


class PointSeriesEntry(BaseModel):
    date: str
    variables: dict[str, VariableValue]


class PointSeriesResponse(BaseModel):
    lat: float | None
    lon: float | None
    series: list[PointSeriesEntry]
