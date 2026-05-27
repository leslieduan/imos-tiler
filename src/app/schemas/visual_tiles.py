from pydantic import BaseModel


class CustomColormapEntry(BaseModel):
    name: str
    mode: str


class ColormapListResponse(BaseModel):
    custom: list[CustomColormapEntry]
    rio_tiler: list[str]
    matplotlib: list[str]


class LegendCategory(BaseModel):
    value: int
    label: str | None = None
    color: list[int]  # [r, g, b, a]


class CategoricalLegendResponse(BaseModel):
    categorical: bool
    variable: str
    categories: list[LegendCategory]
