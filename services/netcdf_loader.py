import logging
import threading
import time

import s3fs
import xarray as xr
from cachetools import LRUCache, cached
from cachetools.keys import hashkey

from constants import COORD_NAMES

logger = logging.getLogger(__name__)

_cache: LRUCache = LRUCache(maxsize=10)
_lock = threading.Lock()


def _fetch(source_path: str, date: str) -> xr.Dataset:
    s3 = s3fs.S3FileSystem(anon=True)
    year = date[:4]
    date_compact = date.replace("-", "")

    file_list = s3.ls(f"{source_path}/{year}/")

    path = next((f for f in file_list if date_compact in f), None)
    if path is None:
        raise FileNotFoundError(f"No file found at '{source_path}' for {date}")


    # xr.open_dataset() only reads metadata (dimensions, coordinates, variable schema). The actual array data is pulled lazily when the renderer accesses variables.
    ds = xr.open_dataset(s3.open(path, timeout=30), engine="h5netcdf")

    rename = {k: v for k, v in COORD_NAMES.items() if k in ds.dims or k in ds.coords}
    if rename:
        ds = ds.rename(rename)

    ds = ds.sel(time=date, method="nearest")

    return ds


# Keyed by (source_path, date) so products sharing the same S3 file share one cached ds object,
# avoiding duplicate HDF5 traversal and variable data downloads across product_ids.
@cached(cache=_cache, key=lambda source_path, date: hashkey(source_path, date), lock=_lock)
def load_dataset(source_path: str, date: str) -> xr.Dataset:
    return _fetch(source_path, date)
