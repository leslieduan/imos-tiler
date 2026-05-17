"""Helpers shared across the three routers (products, data_tiles, visual_tiles).

These previously lived in routers/products.py with leading underscores, and the
other routers reached in past the underscore convention to import them. That
worked but reads as a layering violation — the underscored names announce
"private", yet half the routers package depends on them.

Moving them here keeps products.py focused on its own endpoints and gives the
other routers a public, intentional import target.
"""

from fastapi import HTTPException
from fastapi.openapi.models import Example

from constants import PRODUCTS, Product
from services.loader import load_slice

PRODUCT_EX: dict[str, Example] = {"default": Example(value="sea_level_anomaly")}
DATE_EX: dict[str, Example] = {"default": Example(value="2024-02-24")}


def get_product_or_404(product_id: str) -> Product:
    product = PRODUCTS.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product_id}")
    return product


def load_slice_or_404(store_url: str, date: str, variables: list[str]):
    try:
        return load_slice(store_url, date, variables)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
