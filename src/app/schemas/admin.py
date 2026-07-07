from pydantic import BaseModel


class ProductCreatedResponse(BaseModel):
    id: str
    source_path: str


class ColormapCreatedResponse(BaseModel):
    name: str


class MemoStats(BaseModel):
    current: int
    peak: int
    total_computes: int


class MemoryCacheSizeStats(BaseModel):
    size: int
    max: int


class ProductCacheStats(BaseModel):
    slice_in_flight: int
    processed_in_flight: int


class InFlightStats(BaseModel):
    slice: MemoStats
    processed: MemoStats


class MemoryCacheSizes(BaseModel):
    slice: MemoryCacheSizeStats
    processed: MemoryCacheSizeStats


class CacheStateResponse(BaseModel):
    in_flight: InFlightStats
    memory_cache: MemoryCacheSizes
    products: dict[str, ProductCacheStats]


class MemoryClearedResponse(BaseModel):
    slice: int
    processed: int
