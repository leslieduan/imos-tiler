import logging
import threading

import xarray as xr
from cachetools import LRUCache

# Single Zarr store containing all dates — opened once as a singleton.
# Time selection is lazy (no I/O); actual data is only fetched when .compute() is called.
ZARR_STORE_URL = "s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/"

logger = logging.getLogger(__name__)

_store: xr.Dataset | None = None
_store_lock = threading.Lock()

# Cache of fully-computed 2D (lat × lon) slices keyed by date string.
# Each slice is ~7 MB (4 variables × 351 × 641 × float64); maxsize=20 ≈ 140 MB.
_slice_cache: LRUCache = LRUCache(maxsize=20)
_slice_lock = threading.Lock()


def _get_store() -> xr.Dataset:
    global _store
    with _store_lock:
        if _store is None:
            logger.info("Opening Zarr store: %s", ZARR_STORE_URL)
            _store = xr.open_zarr(ZARR_STORE_URL, storage_options={"anon": True}).sortby("TIME")
    return _store


def load_zarr_slice(date: str) -> xr.Dataset:
    """
    Return a fully-computed 2D (lat × lon) slice for the given date.
    Uses nearest-match on TIME so callers don't need to know exact timestamps.
    Coordinate names are normalised to lat/lon (LATITUDE/LONGITUDE in the store).
    """
    with _slice_lock:
        cached = _slice_cache.get(date)
        if cached is not None:
            return cached

    store = _get_store()
    try:
        logger.info("Zarr compute: date=%s", date)
        ds = store[["GSLA", "UCUR", "VCUR"]].sel(TIME=date, method="nearest").compute()
    except KeyError:
        raise FileNotFoundError(f"No Zarr data found near date {date}")

    ds = ds.rename({"LATITUDE": "lat", "LONGITUDE": "lon"})

    with _slice_lock:
        _slice_cache[date] = ds
    return ds
