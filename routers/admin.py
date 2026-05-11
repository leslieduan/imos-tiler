import os

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

from constants import CHUNK_PX, PADDING
from services.product_store import list_products, register_product, remove_product

_api_key_header = APIKeyHeader(name="X-Admin-Key")


def _require_admin_key(key: str = Security(_api_key_header)) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")
    if key != expected:
        raise HTTPException(status_code=403, detail="Invalid admin key")


router = APIRouter(dependencies=[Depends(_require_admin_key)])


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


@router.get("/products")
def get_products():
    return JSONResponse(content=list_products())


@router.post("/products", status_code=201)
def add_product(payload: ProductPayload):
    try:
        product = register_product(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist product: {e}") from e
    return JSONResponse(
        status_code=201, content={"id": product.id, "source_path": product.source_path}
    )


@router.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: str):
    try:
        remove_product(product_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Product '{product_id}' not found") from e
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist removal: {e}") from e
