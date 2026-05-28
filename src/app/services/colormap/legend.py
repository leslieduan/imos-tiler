"""Color-legend PNG rendering.

A legend is a colored bar plus, optionally, three tick labels (lo, mid, hi).
Continuous colormaps render as a smooth gradient; categorical colormaps render
as equal-width blocks, one per registered category.

Memoised by full argument tuple — legend PNGs are pure functions of their args
and frontends typically request the same legend many times per page load. The
cache is cleared together with the underlying colormap LUTs whenever the custom
colormap registry changes (see [[colormap.resolver]]).
"""

import io
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.services.colormap.registry import is_categorical, on_invalidate
from app.services.colormap.resolver import resolve_colormap

_LABEL_PX = 20  # pixels reserved alongside the bar for tick labels


@lru_cache(maxsize=256)
def render_legend(
    colormap_name: str,
    rescale: tuple[float, float] | None = None,
    width: int = 256,
    height: int = 40,
    orientation: str = "horizontal",
) -> bytes:
    """Render a linear color legend PNG for the given colormap.

    Raises ``ValueError`` (mapped to HTTP 400 by the router) when ``rescale`` is
    given for a categorical colormap: lo/mid/hi tick labels describe a continuous
    scale that discrete categories do not have, so the combination is incoherent.
    """
    categorical = is_categorical(colormap_name)
    if categorical and rescale is not None:
        raise ValueError(
            f"Colormap '{colormap_name}' is categorical; rescale does not apply "
            f"(discrete categories have no continuous scale to label). Omit rescale."
        )
    cm = resolve_colormap(colormap_name)
    lut = np.array([cm[i] for i in range(256)], dtype=np.uint8)
    has_labels = rescale is not None

    if orientation == "horizontal":
        bar_h = max(1, height - _LABEL_PX) if has_labels else height
        bar_w = width
        bar = _build_colorbar(lut, categorical, bar_w, bar_h, vertical=False)
        canvas = np.zeros((height, width, 4), dtype=np.uint8)
        canvas[:bar_h, :] = bar
        if has_labels:
            canvas[bar_h:, :] = [255, 255, 255, 255]
        img = Image.fromarray(canvas, "RGBA")
        if has_labels:
            _draw_h_labels(img, rescale, bar_h, width)  # type: ignore[arg-type]
    else:
        bar_w = max(1, width - _LABEL_PX) if has_labels else width
        bar_h = height
        bar = _build_colorbar(lut, categorical, bar_w, bar_h, vertical=True)
        canvas = np.zeros((height, width, 4), dtype=np.uint8)
        canvas[:, :bar_w] = bar
        if has_labels:
            canvas[:, bar_w:] = [255, 255, 255, 255]
        img = Image.fromarray(canvas, "RGBA")
        if has_labels:
            _draw_v_labels(img, rescale, bar_w, height)  # type: ignore[arg-type]

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _build_colorbar(
    lut: np.ndarray,
    categorical: bool,
    bar_w: int,
    bar_h: int,
    vertical: bool,
) -> np.ndarray:
    """Return a (bar_h, bar_w, 4) uint8 color bar array."""
    bar = np.zeros((bar_h, bar_w, 4), dtype=np.uint8)
    if categorical:
        active = [lut[i] for i in range(256) if lut[i, 3] > 0]
        n = len(active)
        if n:
            dim = bar_h if vertical else bar_w
            for idx, color in enumerate(active):
                d0 = idx * dim // n
                d1 = (idx + 1) * dim // n if idx < n - 1 else dim
                if vertical:
                    bar[d0:d1, :] = color
                else:
                    bar[:, d0:d1] = color
    elif vertical:
        indices = np.round(np.linspace(255, 0, bar_h)).astype(int)
        bar[:] = lut[indices][:, np.newaxis, :]
    else:
        indices = np.round(np.linspace(0, 255, bar_w)).astype(int)
        bar[:] = lut[indices][np.newaxis, :, :]
    return bar


def _draw_h_labels(
    img: Image.Image,
    rescale: tuple[float, float],
    bar_h: int,
    width: int,
) -> None:
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=11)
    lo, hi = rescale
    ticks = [(lo, 0), ((lo + hi) / 2, width // 2), (hi, width - 1)]
    for val, x in ticks:
        draw.line([(x, bar_h), (x, bar_h + 3)], fill=(80, 80, 80, 255))
        label = f"{val:.4g}"
        bbox = font.getbbox(label)
        lw = bbox[2] - bbox[0]
        tx = max(0, min(x - lw // 2, width - lw))
        draw.text((tx, bar_h + 4), label, fill=(0, 0, 0, 255), font=font)


def _draw_v_labels(
    img: Image.Image,
    rescale: tuple[float, float],
    bar_w: int,
    height: int,
) -> None:
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=11)
    lo, hi = rescale
    # vertical: top = hi, bottom = lo
    ticks = [(hi, 0), ((lo + hi) / 2, height // 2), (lo, height - 1)]
    for val, y in ticks:
        draw.line([(bar_w, y), (bar_w + 3, y)], fill=(80, 80, 80, 255))
        label = f"{val:.4g}"
        bbox = font.getbbox(label)
        lh = bbox[3] - bbox[1]
        ty = max(0, min(y - lh // 2, height - lh))
        draw.text((bar_w + 4, ty), label, fill=(0, 0, 0, 255), font=font)


on_invalidate(render_legend.cache_clear)
