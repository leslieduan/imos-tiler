"""RGBA → PNG / WebP encoders shared by the data and visual renderers.

PNG is the only format valid for **data tiles** — the shader decodes RGB bytes
as raw values, so ``optimize=False`` is mandatory (PIL's optimiser can mutate
fully-transparent pixels, which would corrupt the encoded data).

**Visual tiles** also accept WebP. Lossy WebP at quality ~85 gives 40–70%
smaller files than PNG for smooth colour ramps (typical ocean rendering) with
no human-perceptible difference. Lossy WebP is unsuitable for categorical
colormaps (hard colour boundaries get ringing artefacts) — the router rejects
that combination at the request layer.
"""

import io
from typing import Literal

import numpy as np
from PIL import Image

TILE_SIZE = 256

ImageFormat = Literal["png", "webp"]

_WEBP_QUALITY = 85
_WEBP_METHOD = 4  # PIL default; 0=fast/lower-quality, 6=slow/best


def encode_rgba(arr: np.ndarray, fmt: ImageFormat = "png") -> bytes:
    """Encode an (H, W, 4) uint8 RGBA array as PNG or WebP bytes."""
    buf = io.BytesIO()
    img = Image.fromarray(arr, "RGBA")
    if fmt == "webp":
        img.save(buf, format="WEBP", quality=_WEBP_QUALITY, method=_WEBP_METHOD)
    else:
        img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _build_empty_tile(fmt: ImageFormat) -> bytes:
    return encode_rgba(np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8), fmt)


# Returned for tiles outside the data extent. Bytes are immutable, so a single
# instance per format is safe to reuse across all responses — no need to
# re-encode per call.
_EMPTY_TILES: dict[ImageFormat, bytes] = {
    "png": _build_empty_tile("png"),
    "webp": _build_empty_tile("webp"),
}


def empty_tile(fmt: ImageFormat = "png") -> bytes:
    return _EMPTY_TILES[fmt]


def media_type(fmt: ImageFormat) -> str:
    return "image/webp" if fmt == "webp" else "image/png"
