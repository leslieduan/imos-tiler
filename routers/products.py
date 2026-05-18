import hashlib
import math

from fastapi import APIRouter, Header, Path, Query
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse, Response

from constants import CACHE_VERSION, PRODUCTS
from services.loader import get_available_dates
from services.product_config import list_products
from utils.dates import three_months_ago

from .shared import (
    DATE_EX,
    IMMUTABLE_CACHE_HEADERS,
    PRODUCT_EX,
    get_product_or_404,
    load_slice_or_404,
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
    # Automatically sent by browser using previous ETag from prevouse response.
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
