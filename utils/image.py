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
AnimatedFormat = Literal["gif", "apng", "webp"]

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


_EMPTY_TILES: dict[ImageFormat, bytes] = {
    "png": _build_empty_tile("png"),
    "webp": _build_empty_tile("webp"),
}


def empty_tile(fmt: ImageFormat = "png") -> bytes:
    return _EMPTY_TILES[fmt]


def media_type(fmt: ImageFormat) -> str:
    return "image/webp" if fmt == "webp" else "image/png"


def animated_media_type(fmt: AnimatedFormat) -> str:
    if fmt == "gif":
        return "image/gif"
    if fmt == "webp":
        return "image/webp"
    return "image/apng"


def encode_rgba_animation(frames: list[np.ndarray], fmt: AnimatedFormat, duration_ms: int) -> bytes:
    """Encode a sequence of (H, W, 4) uint8 RGBA frames as an animated image.

    GIF quantises the colormap to a 256-colour palette — fine for smooth ramps,
    visibly lossy for categorical. WebP and APNG keep full RGBA fidelity; the
    router rejects WebP for categorical colormaps the same way the static path does.
    """
    if not frames:
        raise ValueError("encode_rgba_animation requires at least one frame")

    images = [Image.fromarray(f, "RGBA") for f in frames]
    head = images[0]
    tail = images[1:]
    buf = io.BytesIO()

    if fmt == "gif":
        # GIF needs paletted frames; PIL handles RGBA → P quantisation when save_all is set.
        head.save(
            buf,
            format="GIF",
            save_all=True,
            append_images=tail,
            duration=duration_ms,
            loop=0,
            disposal=2,
        )
    elif fmt == "webp":
        head.save(
            buf,
            format="WEBP",
            save_all=True,
            append_images=tail,
            duration=duration_ms,
            loop=0,
            quality=_WEBP_QUALITY,
            method=_WEBP_METHOD,
        )
    else:  # apng
        head.save(
            buf,
            format="PNG",
            save_all=True,
            append_images=tail,
            duration=duration_ms,
            loop=0,
        )
    return buf.getvalue()
