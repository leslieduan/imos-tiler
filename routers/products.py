import calendar
import math
from datetime import date

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse

from constants import PRODUCTS
from services.loader import get_available_dates, load_slice
from services.product_store import list_products

router = APIRouter()

_PRODUCT_EX: dict[str, Example] = {"default": Example(value="sea_level_anomaly")}
_DATE_EX: dict[str, Example] = {"default": Example(value="2024-02-24")}


@router.get("/products", summary="List products")
async def get_products():
    return JSONResponse(content=list_products())


def _get_product_or_404(product_id: str):
    product = PRODUCTS.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product_id}")
    return product


def _load_slice_or_404(store_url: str, date: str, variables: list[str]):
    try:
        return load_slice(store_url, date, variables)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


def _three_months_ago() -> str:
    today = date.today()
    month = today.month - 3
    year = today.year
    if month <= 0:
        month += 12
        year -= 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


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
):
    effective_from = from_date or _three_months_ago()
    products = {}
    for product_id, product in PRODUCTS.items():
        dates = get_available_dates(product.source_path)
        dates = [d for d in dates if d >= effective_from]
        if to_date:
            dates = [d for d in dates if d <= to_date]
        products[product_id] = {"available_dates": dates}
    return JSONResponse(content={"products": products})


@router.get(
    "/{product_id}/{date}/point",
    summary="Point value lookup",
    description="Returns the value(s) of all product variables at the nearest grid cell to the given lat/lon.",
)
def get_point(
    product_id: str = Path(openapi_examples=_PRODUCT_EX),
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$", openapi_examples=_DATE_EX),
    lat: float = Query(..., openapi_examples={"default": Example(value=-33.8)}),
    lon: float = Query(..., openapi_examples={"default": Example(value=151.2)}),
):
    product = _get_product_or_404(product_id)
    variables = product.variables
    ds = _load_slice_or_404(product.source_path, date, variables)

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
        }
    )
