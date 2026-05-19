"""Helpers shared across the three routers (products, data_tiles, visual_tiles)."""

from datetime import date as _Date

from fastapi import HTTPException
from fastapi.openapi.models import Example

from constants import PRODUCTS, Product
from services.loader import load_slice

PRODUCT_EX: dict[str, Example] = {"default": Example(value="sea_level_anomaly")}
DATE_EX: dict[str, Example] = {"default": Example(value="2024-02-24")}

# Cache headers for content-addressed endpoints (tiles, legends, per-date manifest,
# point lookups). The URL fully determines the response bytes, so caches can hold the
# response indefinitely. `immutable` blocks browser revalidation on user-triggered
# reload. Invariants: product IDs and colormap names are treated as immutable by admin
# operations; renderer code changes that alter output bytes are propagated by bumping
# CACHE_VERSION (see docs/http_caching.md).
IMMUTABLE_CACHE_HEADERS = {"Cache-Control": f"public, max-age={86400 * 365}, immutable"}


def get_product_or_404(product_id: str) -> Product:
    product = PRODUCTS.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product_id}")
    return product


def validate_date(date: str) -> None:
    try:
        _Date.fromisoformat(date)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid date: {date!r}") from e


def load_slice_or_404(store_url: str, date: str, variables: list[str]):
    try:
        return load_slice(store_url, date, variables)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
