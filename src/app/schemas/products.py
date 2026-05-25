from pydantic import BaseModel


class ProductConfig(BaseModel):
    id: str
    source_path: str
    variable: str | list[str]


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


class TimeseriesPoint(BaseModel):
    date: str
    variables: dict[str, VariableValue]


class TimeseriesResponse(BaseModel):
    lat: float
    lon: float
    series: list[TimeseriesPoint]
