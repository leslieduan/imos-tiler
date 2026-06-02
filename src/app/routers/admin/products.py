"""Admin CRUD for product registrations."""

import asyncio
import logging

import anyio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.config.constants import TILE
from app.schemas.admin import ProductCreatedResponse
from app.services.caching.lifecycle import evict_product_cache, prewarm_disk_slices
from app.services.product.product import Product
from app.services.product.registry import get_product, register_product, remove_product
from app.services.store.registry import get_store

logger = logging.getLogger(__name__)

router = APIRouter()

# Strong refs to background prewarm tasks. asyncio only holds a weak reference to
# tasks created via create_task, so a task with no other reference can be
# garbage-collected mid-run. Keeping it in a module-level set anchors it for the
# lifetime of the prewarm; the done callback removes it once finished.
_background_tasks: set[asyncio.Task] = set()


def _spawn_prewarm(product: Product) -> None:
    task = asyncio.create_task(prewarm_disk_slices([product]))
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            logger.info("Prewarm cancelled", extra={"product_id": product.id})
            return
        exc = t.exception()
        if exc is not None:
            logger.exception(
                "Prewarm failed",
                extra={"product_id": product.id},
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    task.add_done_callback(_on_done)


class CoastalFillPayload(BaseModel):
    """Opt-in coastal fill (see services/rendering/masks.py). Omit to disable."""

    max_dist_px: int

    @field_validator("max_dist_px")
    @classmethod
    def max_dist_px_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v


class ProductPayload(BaseModel):
    id: str
    source_path: str
    variable: str | list[str]
    chunk_px: list[int] = Field(default_factory=lambda: list(TILE.chunk_px))
    padding: int = TILE.padding
    coastal_fill: CoastalFillPayload | None = None

    @field_validator("id")
    @classmethod
    def id_nonempty(cls, v: str) -> str:
        if not v or v != v.strip():
            raise ValueError("must be non-empty with no leading/trailing whitespace")
        return v

    @field_validator("source_path")
    @classmethod
    def source_path_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("chunk_px")
    @classmethod
    def chunk_px_length(cls, v: list[int]) -> list[int]:
        if len(v) != 2 or any(x <= 0 for x in v):
            raise ValueError("must be exactly 2 positive integers")
        return v


def _validate_store(payload: ProductPayload) -> None:
    """Open the store and verify declared variables exist. Raises HTTPException on failure.

    Catches all open failures (network, missing dims, bad scheme, …) and surfaces
    them as 400. Without this, a typo'd URL stays registered and the failure only
    appears in the background prewarm log — confusing for the operator.
    """
    try:
        store = get_store(payload.source_path)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot open store {payload.source_path!r}: {type(e).__name__}: {e}",
        ) from e
    declared = payload.variable if isinstance(payload.variable, list) else [payload.variable]
    missing = [v for v in declared if v not in store.data_vars]
    if missing:
        available = sorted(str(v) for v in store.data_vars)
        raise HTTPException(
            status_code=400,
            detail=f"Variables {missing} not present in store. Available: {available}",
        )


@router.post(
    "/products",
    status_code=201,
    summary="Register a product",
    description=(
        "Registers a new product from a Zarr store and triggers a background cache prewarm. "
        "The store must expose `lat`, `lon`, and `time` coordinates. Returns 409 if the product ID already exists."
    ),
    response_model=ProductCreatedResponse,
)
async def add_product(payload: ProductPayload):
    # Test-open the store before persisting so a bad URL or missing variable
    # surfaces here as 400, not as a silent failure in the background prewarm.
    await anyio.to_thread.run_sync(_validate_store, payload)
    # Offload the sync read+write+reload of products.json so the event loop
    # stays free; the handler itself must remain async because _spawn_prewarm
    # calls asyncio.create_task, which requires a running loop in this thread.
    try:
        product = await anyio.to_thread.run_sync(register_product, payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to persist product", extra={"product_id": payload.id})
        raise HTTPException(status_code=500, detail=f"Failed to persist product: {e}") from e
    logger.info(
        "Product registered",
        extra={"product_id": product.id, "source_path": product.source_path},
    )
    _spawn_prewarm(product)
    return ProductCreatedResponse(id=product.id, source_path=product.source_path)


@router.delete(
    "/products/{product_id}",
    status_code=204,
    summary="Deregister a product",
    description="Removes the product and evicts its cached data. Returns 404 if the product does not exist.",
)
def delete_product(product_id: str):
    product = get_product(product_id)
    try:
        remove_product(product_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Product '{product_id}' not found") from e
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to persist removal of product", extra={"product_id": product_id})
        raise HTTPException(status_code=500, detail=f"Failed to persist removal: {e}") from e
    logger.info("Product removed", extra={"product_id": product_id})
    if product is not None:
        evict_product_cache(product)
