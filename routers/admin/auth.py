"""X-Admin-Key authentication for the admin router.

``auto_error=False`` so we own the missing-key response. With auto_error=True the
framework returns whatever HTTP status it currently uses for a missing key
(today 401, historically and upstream 403) — pinning it here keeps the contract
stable across FastAPI/Starlette upgrades.
"""

import os

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

_api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def require_admin_key(key: str | None = Security(_api_key_header)) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")
    if key is None:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Key header")
    if key != expected:
        raise HTTPException(status_code=403, detail="Invalid admin key")
