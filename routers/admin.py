import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Path, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

from constants import CHUNK_PX, PADDING, PRODUCTS
from services.colormap_store import register_colormap, remove_colormap
from services.loader import evict_product_cache, prewarm_disk_slices
from services.product_store import register_product, remove_product

_api_key_header = APIKeyHeader(name="X-Admin-Key")


def _require_admin_key(key: str = Security(_api_key_header)) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")
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
    asyncio.create_task(asyncio.to_thread(prewarm_disk_slices, [product]))
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


def _hex_to_rgba(hex_color: str) -> list[int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 6:
        return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255]
    if len(h) == 8:
        return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)]
    raise ValueError(f"invalid hex color: {hex_color!r}")


def _interpolate_colormap(stops: list[list[int]]) -> list[list[int]]:
    """Linearly interpolate N evenly-spaced RGBA stops to 256 entries."""
    import numpy as np

    arr = np.array(stops, dtype=float)
    x_stops = np.linspace(0, 1, len(stops))
    x_out = np.linspace(0, 1, 256)
    interpolated = np.stack([np.interp(x_out, x_stops, arr[:, c]) for c in range(4)], axis=1)
    return np.clip(interpolated, 0, 255).round().astype(int).tolist()  # type: ignore[no-any-return]


class ColormapPayload(BaseModel):
    name: str
    entries: list[list[int]] = Field(
        ...,
        description=(
            "2–256 RGBA color stops. Each entry is either a list of 4 ints [r, g, b, a] "
            "in [0, 255], or a CSS hex string (#rgb, #rrggbb, #rrggbbaa). "
            "Hex strings without alpha default to fully opaque (a=255). "
            "Stops are treated as evenly spaced; fewer than 256 are linearly interpolated "
            "to fill all 256 LUT slots."
        ),
    )

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v or v != v.strip():
            raise ValueError("must be non-empty with no leading/trailing whitespace")
        return v

    @field_validator("entries", mode="before")
    @classmethod
    def entries_valid(cls, v: list) -> list[list[int]]:
        if len(v) < 2:
            raise ValueError(f"entries must have at least 2 color stops, got {len(v)}")
        if len(v) > 256:
            raise ValueError(f"entries must have at most 256 items, got {len(v)}")
        normalized: list[list[int]] = []
        for i, entry in enumerate(v):
            if isinstance(entry, str):
                try:
                    rgba = _hex_to_rgba(entry)
                except ValueError as e:
                    raise ValueError(f"entry {i}: {e}") from e
            elif isinstance(entry, list | tuple):
                rgba = list(entry)
            else:
                raise ValueError(f"entry {i} must be a hex string or [r, g, b, a] list")
            if len(rgba) != 4 or not all(isinstance(c, int) and 0 <= c <= 255 for c in rgba):
                raise ValueError(f"entry {i} must be [r, g, b, a] with values 0–255")
            normalized.append(rgba)
        return normalized if len(normalized) == 256 else _interpolate_colormap(normalized)

    def to_tuples(self) -> list[tuple[int, int, int, int]]:
        return [(rgba[0], rgba[1], rgba[2], rgba[3]) for rgba in self.entries]


@admin_router.post(
    "/colormaps",
    status_code=201,
    summary="Register a custom colormap",
    description="Registers a named colormap from 256 RGBA entries. Returns 409 if the name already exists.",
)
def add_colormap(payload: ColormapPayload):
    try:
        register_colormap(payload.name, payload.to_tuples())
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
