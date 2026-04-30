import logging
import threading
from datetime import datetime, timezone

import numpy as np
import s3fs
import xarray as xr
from cachetools import LRUCache, cached
from cachetools.keys import hashkey

from constants import COORD_NAMES, PRODUCTS, Product

logger = logging.getLogger(__name__)

_cache: LRUCache = LRUCache(maxsize=10)
_lock = threading.Lock()


def _fetch(product: Product, date: str) -> xr.Dataset:
    s3 = s3fs.S3FileSystem(anon=True)
    year = date[:4]
    date_compact = date.replace("-", "")
    file_list = s3.ls(f"{product.source_path}/{year}/")
    path = next((f for f in file_list if date_compact in f), None)
    if path is None:
        raise FileNotFoundError(f"No file found for product '{product.id}' on {date}")

    logger.info("S3 fetch: %s", path)
    ds = xr.open_dataset(s3.open(path), engine="h5netcdf")

    rename = {k: v for k, v in COORD_NAMES.items() if k in ds.dims or k in ds.coords}
    if rename:
        ds = ds.rename(rename)

    if np.issubdtype(ds.time.dtype, np.integer):
        ts = int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        ds = ds.sel(time=ts, method="nearest")
    else:
        ds = ds.sel(time=date)

    return ds


@cached(cache=_cache, key=lambda product_id, date: hashkey(product_id, date), lock=_lock)
def load_dataset(product_id: str, date: str) -> xr.Dataset:
    product = PRODUCTS[product_id]
    return _fetch(product, date)
