"""Helpers shared across the three routers (products, data_tiles, visual_tiles)."""

from datetime import date as _Date

from fastapi import HTTPException
from fastapi.openapi.models import Example

from app.services.caching.slice_cache import load_slice
from app.services.colormap.categorical import is_categorical_variable
from app.services.colormap.registry import get_category_values, is_categorical
from app.services.colormap.resolver import resolve_colormap
from app.services.product.product import Product
from app.services.product.registry import get_product
from app.services.store.registry import get_store

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
    product = get_product(product_id)
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


def resolve_colormap_or_error(name: str, *, status_code: int = 400) -> None:
    """Validate a colormap name, raising HTTPException on failure.

    Defaults to 400 (colormap usually arrives as a query param, so an unknown
    name is a malformed request). Callers exposing it as a path segment pass
    status_code=404 — the URL points at a resource that does not exist.
    """
    try:
        resolve_colormap(name)
    except ValueError as e:
        raise HTTPException(status_code=status_code, detail=str(e)) from e


def single_variable_or_400(product: Product, *, context: str) -> str:
    """Narrow product.variable to a single str, rejecting multi-variable products."""
    if isinstance(product.variable, list):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Product '{product.id}' has multiple variables; "
                f"{context} supports single-variable products only."
            ),
        )
    return product.variable


def parse_rescale(rescale: str | None) -> tuple[float, float] | None:
    if not rescale:
        return None
    try:
        lo, hi = rescale.split(",")
        return (float(lo), float(hi))
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail="rescale must be 'min,max', e.g. '-0.5,0.5'"
        ) from e


def reject_webp_for_categorical(
    colormap_name: str | None, fmt: str, *, animated: bool = False
) -> None:
    # Lossy WebP introduces ringing/blocking around the hard colour boundaries
    # of a categorical colormap. PNG (or APNG/GIF for animations) is the only
    # safe choice — fail loud rather than serve a corrupted legend.
    if fmt != "webp" or not colormap_name or not is_categorical(colormap_name):
        return
    kind = "animated WebP" if animated else "WebP"
    alternatives = "Use .apng or .gif." if animated else "Use .png."
    raise HTTPException(
        status_code=400,
        detail=(
            f"Colormap '{colormap_name}' is categorical and cannot be encoded as {kind} "
            f"(lossy compression corrupts the discrete colour boundaries). {alternatives}"
        ),
    )


def reject_categorical_colormap_mismatch(
    colormap_name: str | None, product: Product, variable: str
) -> None:
    """Reject a categorical colormap that doesn't fit the target variable.

    A categorical colormap encodes a fixed set of integer codes and renders only
    through the discrete, value-indexed LUT path — which exists only for categorical
    variables (those with CF flag_values). So a categorical colormap requires:

      1. a categorical variable — on a continuous variable it would fall through to
         the continuous ramp path, where its colours land on scale-dependent slots
         that drift per tile (the reason rescale used to be mandatory); and
      2. category values equal to the variable's flag_values — otherwise its colours
         map to the wrong codes, silently, since the discrete LUT still renders fine.

    Both are 400s. Only fires for a categorical colormap; a continuous colormap on a
    categorical variable is left to the renderer's own guard. The variable's attrs are
    read from the store only when a categorical colormap is actually requested, so the
    common path is untouched.
    """
    if not colormap_name or not is_categorical(colormap_name):
        return
    try:
        attrs = get_store(product.source_path)[variable].attrs
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if not is_categorical_variable(attrs):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Categorical colormap '{colormap_name}' can only be applied to a categorical "
                f"variable (one with CF flag_values); product '{product.id}' variable "
                f"'{variable}' is continuous."
            ),
        )
    expected = sorted(int(v) for v in attrs["flag_values"])
    cmap_values = get_category_values(colormap_name)
    if cmap_values != expected:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Categorical colormap '{colormap_name}' covers values {cmap_values}, which do "
                f"not match product '{product.id}' variable '{variable}' flag_values {expected}."
            ),
        )
