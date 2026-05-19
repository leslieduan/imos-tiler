import asyncio
import hashlib
import math

from fastapi import APIRouter, Header, Path, Query
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse, Response

from constants import CACHE_VERSION, PRODUCTS
from services.loader import get_available_dates, load_slice
from services.product_config import list_products
from utils.dates import three_months_ago

from .shared import (
    DATE_EX,
    IMMUTABLE_CACHE_HEADERS,
    PRODUCT_EX,
    get_product_or_404,
    load_slice_or_404,
    validate_date,
)

router = APIRouter()

# Manifest responses are revalidated via ETag (If-None-Match → 304 when unchanged), with a
# 5-minute freshness window so CloudFront can absorb concurrent reads from multiple users
# without each one round-tripping to origin. Therefore, this endpoint need to be cached in CloudFront
# with "must-revalidate" to ensure clients re-check with the origin at least every 5 minutes.
# Trade-off: a manifest change can be invisible for up to 5 minutes; acceptable because product/date
# updates are not real-time-critical.
_REVALIDATE_HEADERS = {"Cache-Control": "public, max-age=300, must-revalidate"}


def _etag(fingerprint: str) -> str:
    digest = hashlib.sha1(fingerprint.encode(), usedforsecurity=False).hexdigest()[:16]
    return f'W/"{digest}"'


def _etag_response(body: object, etag: str, if_none_match: str | None) -> Response:
    headers = {**_REVALIDATE_HEADERS, "ETag": etag}
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=body, headers=headers)


@router.get("/products", summary="List products")
async def get_products():
    return JSONResponse(content=list_products())


@router.get(
    "/manifest",
    summary="Products availability",
    description=(
        "Returns available dates for every product. "
        "`from` defaults to 3 months before today; `to` is unbounded by default."
    ),
)
def get_products_availability(
    from_date: str | None = Query(
        None,
        alias="from",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Start date (inclusive), YYYY-MM-DD. Defaults to 3 months before today.",
        openapi_examples={"default": Example(value="2024-01-01")},
    ),
    to_date: str | None = Query(
        None,
        alias="to",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="End date (inclusive), YYYY-MM-DD. Defaults to no upper bound.",
        openapi_examples={"default": Example(value="2024-12-31")},
    ),
    if_none_match: str | None = Header(None, alias="if-none-match"),
    # Automatically sent by browser using previous ETag from previous response.
):
    effective_from = from_date or three_months_ago()
    products = {}

    fingerprint_parts = [
        f"cv={CACHE_VERSION}",
        f"from={effective_from}",
        f"to={to_date or ''}",
    ]
    # Snapshot first: a concurrent admin reload mutating PRODUCTS during iteration
    # would otherwise raise RuntimeError ("dictionary changed size during iteration").
    for product_id, product in list(PRODUCTS.items()):
        dates = get_available_dates(product.source_path)
        dates = [d for d in dates if d >= effective_from]
        if to_date:
            dates = [d for d in dates if d <= to_date]
        products[product_id] = {"available_dates": dates}
        fingerprint_parts.append(f"{product_id}:{len(dates)}:{dates[-1] if dates else ''}")

    etag = _etag("|".join(fingerprint_parts))
    return _etag_response(
        {"products": products, "cache_version": CACHE_VERSION}, etag, if_none_match
    )


@router.get(
    "/{product_id}/{date}/point",
    summary="Point value lookup",
    description="Returns the value(s) of all product variables at the nearest grid cell to the given lat/lon.",
)
def get_point(
    product_id: str = Path(openapi_examples=PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=DATE_EX),
    lat: float = Query(..., openapi_examples={"default": Example(value=-33.8)}),
    lon: float = Query(..., openapi_examples={"default": Example(value=151.2)}),
):
    product = get_product_or_404(product_id)
    validate_date(date)
    variables = product.variables
    ds = load_slice_or_404(product.source_path, date, variables)

    point = ds.sel(lat=lat, lon=lon, method="nearest")

    values = {}
    for var in variables:
        v = float(point[var].squeeze())
        values[var] = {
            "value": None if math.isnan(v) else v,
            "units": point[var].attrs.get("units"),
        }

    return JSONResponse(
        content={
            "lat": float(point.lat.values),
            "lon": float(point.lon.values),
            "variables": values,
        },
        headers=IMMUTABLE_CACHE_HEADERS,
    )


@router.get(
    "/{product_id}/point",
    summary="Point value time series",
    description=(
        "Returns the value(s) of all product variables at the nearest grid cell to the "
        "given lat/lon across a date range. `from` defaults to 3 months before today; "
        "`to` is unbounded by default. Only dates with available data are included."
    ),
)
# async because we want to parallelise the per-date load_slice calls, which are the bottleneck for a multi-date request.
async def get_point_series(
    product_id: str = Path(openapi_examples=PRODUCT_EX),
    lat: float = Query(..., openapi_examples={"default": Example(value=-33.8)}),
    lon: float = Query(..., openapi_examples={"default": Example(value=151.2)}),
    from_date: str | None = Query(
        None,
        alias="from",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Start date (inclusive), YYYY-MM-DD. Defaults to 3 months before today.",
        openapi_examples={"default": Example(value="2024-01-01")},
    ),
    to_date: str | None = Query(
        None,
        alias="to",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="End date (inclusive), YYYY-MM-DD. Defaults to no upper bound.",
        openapi_examples={"default": Example(value="2024-01-31")},
    ),
):
    product = get_product_or_404(product_id)
    variables = product.variables

    effective_from = from_date or three_months_ago()
    available = get_available_dates(product.source_path)
    dates_in_range = [d for d in available if d >= effective_from and (not to_date or d <= to_date)]

    # Fan out load_slice across dates: each call is IO/CPU-bound on the Zarr store, so
    # asyncio.to_thread frees the event loop while they run. Concurrent identical
    # requests still share one compute via the slice memoizer in load_slice.
    slices = await asyncio.gather(
        *(asyncio.to_thread(load_slice, product.source_path, d, variables) for d in dates_in_range)
    )

    series = []
    point_lat: float | None = None
    point_lon: float | None = None
    for date_str, ds in zip(dates_in_range, slices, strict=True):
        point = ds.sel(lat=lat, lon=lon, method="nearest")
        if point_lat is None:
            point_lat = float(point.lat.values)
            point_lon = float(point.lon.values)
        values = {}
        for var in variables:
            v = float(point[var].squeeze())
            values[var] = {
                "value": None if math.isnan(v) else v,
                "units": point[var].attrs.get("units"),
            }
        series.append({"date": date_str, "variables": values})

    # Revalidate (not immutable): an unbounded `to` window will pick up new dates as
    # they land, so we can't promise the response bytes are pinned to the URL.
    return JSONResponse(
        content={"lat": point_lat, "lon": point_lon, "series": series},
        headers=_REVALIDATE_HEADERS,
    )
