from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from constants import CHUNK_PX, PADDING
from services.product_store import list_products, register_product, remove_product

router = APIRouter()


class ProductPayload(BaseModel):
    id: str
    source_path: str
    variable: str | list[str] = ""
    chunk_px: list[int] = Field(default_factory=lambda: list(CHUNK_PX))
    padding: int = PADDING


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
