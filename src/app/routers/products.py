import asyncio
import hashlib
import math

from fastapi import APIRouter, Header, Path, Query, Response
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse

from app.constants import CACHE_VERSION, PRODUCTS
from app.schemas.products import (
    ManifestResponse,
    PointResponse,
    PointSeriesEntry,
    PointSeriesResponse,
    ProductConfig,
    VariableValue,
)
from app.services.loader import get_available_dates, load_slice
from app.services.product_config import list_products
from app.utils.dates import three_months_ago

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


@router.get("/products", summary="List products", response_model=list[ProductConfig])
async def get_products():
    return [ProductConfig(**p) for p in list_products()]


@router.get(
    "/manifest",
    summary="Products availability",
    description=(
        "Returns available dates for every product. "
        "`from` defaults to 3 months before today; `to` is unbounded by default."
    ),
    # response_model=ManifestResponse,  # can't use this because of the dynamic ETag-based 304 response
    responses={
        200: {"model": ManifestResponse},
        304: {"description": "Not Modified — ETag matched, response body is empty"},
    },
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
    response_model=PointResponse,
)
def get_point(
    response: Response,
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

    values: dict[str, VariableValue] = {}
    for var in variables:
        v = float(point[var].squeeze())
        values[var] = VariableValue(
            value=None if math.isnan(v) else v,
            units=point[var].attrs.get("units"),
        )

    response.headers.update(IMMUTABLE_CACHE_HEADERS)
    return PointResponse(
        lat=float(point.lat.values),
        lon=float(point.lon.values),
        variables=values,
    )


@router.get(
    "/{product_id}/point",
    summary="Point value time series",
    description=(
        "Returns the value(s) of all product variables at the nearest grid cell to the "
        "given lat/lon across a date range. `from` defaults to 3 months before today; "
        "`to` is unbounded by default. Only dates with available data are included."
    ),
    response_model=PointSeriesResponse,
)
# async because we want to parallelise the per-date load_slice calls, which are the bottleneck for a multi-date request.
async def get_point_series(
    response: Response,
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
        values: dict[str, VariableValue] = {}
        for var in variables:
            v = float(point[var].squeeze())
            values[var] = VariableValue(
                value=None if math.isnan(v) else v,
                units=point[var].attrs.get("units"),
            )
        series.append(PointSeriesEntry(date=date_str, variables=values))

    # Revalidate (not immutable): an unbounded `to` window will pick up new dates as
    # they land, so we can't promise the response bytes are pinned to the URL.
    response.headers.update(_REVALIDATE_HEADERS)
    return PointSeriesResponse(lat=point_lat, lon=point_lon, series=series)
