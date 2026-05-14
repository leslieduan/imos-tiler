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
    variable: str | list[str] = ""
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


@admin_router.post("/products", status_code=201)
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


@admin_router.delete("/products/{product_id}", status_code=204)
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
    entries: list[list[int]] = Field(
        ..., description="Exactly 256 RGBA entries, each a list of 4 ints in [0, 255]."
    )

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v or v != v.strip():
            raise ValueError("must be non-empty with no leading/trailing whitespace")
        return v

    @field_validator("entries")
    @classmethod
    def entries_valid(cls, v: list[list[int]]) -> list[list[int]]:
        if len(v) != 256:
            raise ValueError(f"entries must have exactly 256 items, got {len(v)}")
        for i, rgba in enumerate(v):
            if len(rgba) != 4 or not all(0 <= c <= 255 for c in rgba):
                raise ValueError(f"entry {i} must be [r, g, b, a] with values 0–255")
        return v

    def to_tuples(self) -> list[tuple[int, int, int, int]]:
        return [(rgba[0], rgba[1], rgba[2], rgba[3]) for rgba in self.entries]


@admin_router.post("/colormaps", status_code=201)
def add_colormap(payload: ColormapPayload):
    try:
        register_colormap(payload.name, payload.to_tuples())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist colormap: {e}") from e
    return JSONResponse(status_code=201, content={"name": payload.name})


@admin_router.delete("/colormaps/{name}", status_code=204)
def delete_colormap(name: str = Path(...)):
    try:
        remove_colormap(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Colormap '{name}' not found") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist removal: {e}") from e
