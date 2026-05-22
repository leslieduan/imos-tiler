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
    file_count: int
    total_bytes: int
    oldest_date: str | None
    newest_date: str | None
    last_write_at: str | None
    files: list[str]
    cache_dir: str
    slice_in_flight: int
    processed_in_flight: int


class InFlightStats(BaseModel):
    slice: MemoStats
    processed: MemoStats


class MemoryCacheSizes(BaseModel):
    slice: MemoryCacheSizeStats
    processed: MemoryCacheSizeStats


class PrewarmStatus(BaseModel):
    running: bool


class RefreshStatus(BaseModel):
    status: str
    last_started_at: str | None
    last_completed_at: str | None
    last_error: str | None
    interval_seconds: int


class DiskWritesStatus(BaseModel):
    prewarm: PrewarmStatus
    refresh: RefreshStatus


class GlobalDiskStats(BaseModel):
    base_path: str
    total_bytes: int
    limit_bytes: int
    eviction_threshold_bytes: int
    utilization_pct: float
    over_eviction_threshold: bool


class CacheStateResponse(BaseModel):
    disk: GlobalDiskStats
    disk_writes: DiskWritesStatus
    in_flight: InFlightStats
    memory_cache: MemoryCacheSizes
    products: dict[str, ProductCacheStats]


class MemoryClearedResponse(BaseModel):
    slice: int
    processed: int


class DiskClearedResponse(BaseModel):
    files: int
    directories: int
