"""PNG encoding helpers shared by the data and visual renderers.

Both renderers ultimately call ``Image.fromarray(rgba, "RGBA").save(buf, "PNG")``
with ``optimize=False``. The flag matters: the data tile shader decodes RGB bytes
as raw values, so any post-processing that mutates pixels (which PIL's optimiser
can do for fully-transparent regions) would corrupt the encoded data. Keeping a
single helper means that contract is set in one place.
"""

import io

import numpy as np
from PIL import Image

TILE_SIZE = 256


def encode_rgba(arr: np.ndarray) -> bytes:
    """Encode an (H, W, 4) uint8 RGBA array as PNG bytes."""
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _build_empty_tile() -> bytes:
    return encode_rgba(np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8))


# Returned for tiles outside the data extent. Bytes are immutable, so a single
# instance is safe to reuse across all responses — no need to re-encode per call.
EMPTY_RGBA_TILE: bytes = _build_empty_tile()
