"""utils.image: encoder behaviour tests.

Static encoding is covered indirectly by the renderer tests; this file exercises
the animated encoder in isolation so failures point at the encoder rather than
the renderer stack.
"""

import io

import numpy as np
import pytest
from PIL import Image

from app.utils.image import animated_media_type, encode_rgba_animation


def _frame(value: int, size: int = 32) -> np.ndarray:
    arr = np.full((size, size, 4), value, dtype=np.uint8)
    arr[..., 3] = 255
    return arr


def test_encode_apng_preserves_frame_count():
    frames = [_frame(50), _frame(150), _frame(250)]
    data = encode_rgba_animation(frames, "apng", duration_ms=120)
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG"
    assert getattr(img, "n_frames", 1) == 3


def test_encode_webp_preserves_frame_count():
    frames = [_frame(50), _frame(150)]
    data = encode_rgba_animation(frames, "webp", duration_ms=200)
    img = Image.open(io.BytesIO(data))
    assert img.format == "WEBP"
    assert getattr(img, "n_frames", 1) == 2


def test_encode_gif_preserves_frame_count():
    frames = [_frame(50), _frame(150), _frame(250)]
    data = encode_rgba_animation(frames, "gif", duration_ms=200)
    img = Image.open(io.BytesIO(data))
    assert img.format == "GIF"
    assert getattr(img, "n_frames", 1) == 3


def test_encode_rejects_empty_frame_list():
    with pytest.raises(ValueError):
        encode_rgba_animation([], "apng", duration_ms=200)


def test_animated_media_type_mapping():
    assert animated_media_type("gif") == "image/gif"
    assert animated_media_type("webp") == "image/webp"
    assert animated_media_type("apng") == "image/apng"
