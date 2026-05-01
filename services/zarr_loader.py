import logging
import threading

import xarray as xr
from cachetools import LRUCache

from constants import COORD_NAMES, DEFAULT_ZARR_LOD_GRIDS, Product
from services.utils import compute_lod_grids

logger = logging.getLogger(__name__)

# Zarr stores opened once per URL and reused across requests.
_stores: dict[str, xr.Dataset] = {}
_store_lock = threading.Lock()

# Cache of fully-computed 2D (lat × lon) slices keyed by (store_url, date, variables).
# Each slice is ~7 MB (4 variables × 351 × 641 × float64); maxsize=20 ≈ 140 MB.
_slice_cache: LRUCache = LRUCache(maxsize=20)
_slice_lock = threading.Lock()


def _get_store(store_url: str) -> xr.Dataset:
    with _store_lock:
        if store_url not in _stores:
            _stores[store_url] = xr.open_zarr(store_url, storage_options={"anon": True}).sortby("TIME")
    return _stores[store_url]


def get_lod_grids(product: Product) -> dict[int, tuple[int, int]]:
    """
    Ensure product.lod_grids is populated from actual store dimensions, then return it.
    Writes back to product on first call so subsequent callers find it already set.
    """
    if product.lod_grids:
        return product.lod_grids

    store = _get_store(product.source_path)
    lat_dim = next((d for d in ("lat", "LATITUDE") if d in store.dims), None)
    lon_dim = next((d for d in ("lon", "LONGITUDE") if d in store.dims), None)

    if lat_dim is None or lon_dim is None:
        object.__setattr__(product, 'lod_grids', DEFAULT_ZARR_LOD_GRIDS)
        return product.lod_grids

    data_height = store.dims[lat_dim]
    data_width = store.dims[lon_dim]
    grids = compute_lod_grids(data_width, data_height, product.chunk_px)
    logger.info(
        "Computed LOD grids for %s: data=%dx%d chunk=%s → %s",
        product.id, data_width, data_height, product.chunk_px, grids,
    )
    object.__setattr__(product, 'lod_grids', grids)
    return product.lod_grids


def load_zarr_slice(store_url: str, date: str, variables: list[str]) -> xr.Dataset:
    """
    Return a fully-computed 2D (lat × lon) slice for the given store, date, and variables.
    Uses nearest-match on TIME so callers don't need to know exact timestamps.
    Coordinate names are normalised to lat/lon (LATITUDE/LONGITUDE in the store).
    """
    cache_key = (store_url, date, tuple(sorted(variables)))
    with _slice_lock:
        cached = _slice_cache.get(cache_key)
        if cached is not None:
            return cached

    store = _get_store(store_url)
    try:
        ds = store[variables].sel(TIME=date, method="nearest").compute()
    except KeyError:
        raise FileNotFoundError(f"No Zarr data found near date {date}")

    rename = {k: v for k, v in COORD_NAMES.items() if k in ds.dims or k in ds.coords}
    if rename:
        ds = ds.rename(rename)

    with _slice_lock:
        _slice_cache[cache_key] = ds
    return ds
