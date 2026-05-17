import asyncio
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Path, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator, model_validator

from constants import CHUNK_PX, PADDING, PRODUCTS, Product
from services.colormap_store import ColormapMode, register_colormap, remove_colormap
from services.loader import evict_product_cache, prewarm_disk_slices
from services.product_store import register_product, remove_product
from utils.colors import build_categorical_lut, interpolate_colormap, parse_color

logger = logging.getLogger(__name__)

# Strong refs to background prewarm tasks. asyncio only holds a weak reference to
# tasks created via create_task, so a task with no other reference can be
# garbage-collected mid-run. Keeping it in a module-level set anchors it for the
# lifetime of the prewarm; the done callback removes it once finished.
_background_tasks: set[asyncio.Task] = set()


def _spawn_prewarm(product: Product) -> None:
    task = asyncio.create_task(asyncio.to_thread(prewarm_disk_slices, [product]))
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            logger.info("Prewarm cancelled for %s", product.id)
            return
        exc = t.exception()
        if exc is not None:
            logger.exception(
                "Prewarm failed for %s", product.id, exc_info=(type(exc), exc, exc.__traceback__)
            )

    task.add_done_callback(_on_done)


# auto_error=False so we own the missing-key response. With auto_error=True the
# framework returns whatever HTTP status it currently uses for a missing key
# (today 401, historically and upstream 403) — pinning it here keeps the contract
# stable across FastAPI/Starlette upgrades.
_api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def _require_admin_key(key: str | None = Security(_api_key_header)) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")
    if key is None:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Key header")
    if key != expected:
        raise HTTPException(status_code=403, detail="Invalid admin key")


admin_router = APIRouter(dependencies=[Depends(_require_admin_key)])


class ProductPayload(BaseModel):
    id: str
    source_path: str
    variable: str | list[str]
    chunk_px: list[int] = Field(default_factory=lambda: list(CHUNK_PX))
    padding: int = PADDING

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


@admin_router.post(
    "/products",
    status_code=201,
    summary="Register a product",
    description=(
        "Registers a new product from a Zarr store and triggers a background cache prewarm. "
        "The store must expose `lat`, `lon`, and `time` coordinates. Returns 409 if the product ID already exists."
    ),
)
async def add_product(payload: ProductPayload):
    try:
        product = register_product(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist product: {e}") from e
    _spawn_prewarm(product)
    return JSONResponse(
        status_code=201, content={"id": product.id, "source_path": product.source_path}
    )


@admin_router.delete(
    "/products/{product_id}",
    status_code=204,
    summary="Deregister a product",
    description="Removes the product and evicts its cached data. Returns 404 if the product does not exist.",
)
def delete_product(product_id: str):
    product = PRODUCTS.get(product_id)
    try:
        remove_product(product_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Product '{product_id}' not found") from e
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist removal: {e}") from e
    if product is not None:
        evict_product_cache(product)


class ColormapPayload(BaseModel):
    name: str
    mode: ColormapMode = Field(
        default="ramp",
        description=(
            "'ramp': evenly-spaced stops, linearly interpolated to 256 LUT entries. Dataset-agnostic. "
            "'categorical': discrete integer value→color mapping (equivalent to CF flag_values+flag_colors). "
            "Dataset-specific — the entry keys must exactly match the integer values present in the dataset. "
            "Applying a categorical colormap to a dataset with different values renders without error "
            "but produces silently wrong colours. Name categorical colormaps after the dataset or variable "
            "they describe to make the coupling explicit."
        ),
    )
    entries: list[list[int]] = Field(
        ...,
        description=(
            "ramp mode — 2–256 color stops (hex string or [r,g,b,a] list), interpolated to 256. "
            "categorical mode — dict mapping integer data values to colors, "
            'e.g. {"1": "#ff0000", "2": [0, 0, 255, 255]}. '
            "Keys must match the exact integer values in the target dataset variable."
        ),
    )

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v or v != v.strip():
            raise ValueError("must be non-empty with no leading/trailing whitespace")
        return v

    @model_validator(mode="before")
    @classmethod
    def build_lut(cls, data: dict) -> dict:
        mode = data.get("mode", "ramp")
        raw = data.get("entries")
        if isinstance(raw, dict) and mode != "categorical":
            raise ValueError(
                f'entries is a dict, which is only valid for mode="categorical" '
                f'(current mode: {mode!r}). Set "mode": "categorical" or pass a list of colour stops.'
            )
        if mode != "categorical":
            return data
        raw = data.get("entries")
        if not isinstance(raw, dict):
            raise ValueError('entries must be a dict for categorical mode, e.g. {"1": "#ff0000"}')
        categories: dict[int, list[int]] = {}
        for k, v in raw.items():
            try:
                val = int(k)
            except (ValueError, TypeError) as e:
                raise ValueError(f"entries key {k!r} must be an integer") from e
            categories[val] = parse_color(v, f"entries[{k!r}]")
        data_range = (float(min(categories)), float(max(categories)))
        data["entries"] = build_categorical_lut(categories, data_range)
        return data

    @field_validator("entries", mode="before")
    @classmethod
    def entries_valid(cls, v: list) -> list[list[int]]:
        if len(v) < 2:
            raise ValueError(f"entries must have at least 2 color stops, got {len(v)}")
        if len(v) > 256:
            raise ValueError(f"entries must have at most 256 items, got {len(v)}")
        normalized: list[list[int]] = []
        for i, entry in enumerate(v):
            try:
                rgba = parse_color(entry, f"entry {i}")
            except ValueError as e:
                raise ValueError(f"entry {i}: {e}") from e
            normalized.append(rgba)
        return normalized if len(normalized) == 256 else interpolate_colormap(normalized)

    def to_tuples(self) -> list[tuple[int, int, int, int]]:
        return [(rgba[0], rgba[1], rgba[2], rgba[3]) for rgba in self.entries]


@admin_router.post(
    "/colormaps",
    status_code=201,
    summary="Register a custom colormap",
    description="Registers a named colormap from 2–256 color stops, interpolated to 256 entries. Returns 409 if the name already exists.",
)
def add_colormap(payload: ColormapPayload):
    try:
        register_colormap(payload.name, payload.to_tuples(), payload.mode)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist colormap: {e}") from e
    return JSONResponse(status_code=201, content={"name": payload.name})


@admin_router.delete(
    "/colormaps/{name}",
    status_code=204,
    summary="Remove a custom colormap",
    description="Removes a previously registered custom colormap. Returns 404 if not found.",
)
def delete_colormap(name: str = Path(...)):
    try:
        remove_colormap(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Colormap '{name}' not found") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist removal: {e}") from e
