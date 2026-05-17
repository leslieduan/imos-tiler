"""Admin endpoints, gated behind X-Admin-Key auth.

Two CRUD domains (products and colormaps) share only the auth dependency; each
lives in its own submodule. The top-level router applies the auth dependency
once so neither submodule has to remember to.
"""

from fastapi import APIRouter, Depends

from .auth import require_admin_key
from .colormaps import router as colormaps_router
from .products import router as products_router

admin_router = APIRouter(dependencies=[Depends(require_admin_key)])
admin_router.include_router(products_router)
admin_router.include_router(colormaps_router)
