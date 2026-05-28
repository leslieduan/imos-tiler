from pydantic import BaseModel


class CustomColormapEntry(BaseModel):
    name: str
    mode: str


class ColormapListResponse(BaseModel):
    custom: list[CustomColormapEntry]
    rio_tiler: list[str]
    matplotlib: list[str]
