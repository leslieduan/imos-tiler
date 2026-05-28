"""Color-legend PNG rendering: ramp vs categorical, orientations, labels.

Decodes the produced PNG and asserts dimensions + key pixel characteristics —
that's enough to catch regressions in layout without making the tests pixel-fragile.
"""

import io

import numpy as np
import pytest
from PIL import Image

import app.services.colormap.legend as legend_renderer


@pytest.fixture(autouse=True)
def clear_legend_cache():
    """The legend renderer is lru_cached — strict isolation between tests."""
    legend_renderer.render_legend.cache_clear()
    yield
    legend_renderer.render_legend.cache_clear()


def _decode(png: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(png)))


def test_horizontal_no_rescale_returns_correct_size():
    png = legend_renderer.render_legend("viridis", width=256, height=40)
    img = _decode(png)
    assert img.shape == (40, 256, 4)


def test_vertical_no_rescale_returns_correct_size():
    png = legend_renderer.render_legend("viridis", width=40, height=256, orientation="vertical")
    img = _decode(png)
    assert img.shape == (256, 40, 4)


def test_horizontal_with_rescale_reserves_label_strip():
    """With labels, the bottom 20px must contain text (dark pixels on white bg)."""
    png = legend_renderer.render_legend("viridis", rescale=(0.0, 1.0), width=256, height=60)
    img = _decode(png)
    assert img.shape == (60, 256, 4)
    label_strip = img[-20:, :, :3]
    # Bottom strip is mostly white background with some dark text — assert dark pixels exist.
    dark_pixel_count = (label_strip.sum(axis=-1) < 200).sum()
    assert dark_pixel_count > 10, "no dark text pixels found in the label strip"


def test_vertical_with_rescale_reserves_right_strip():
    png = legend_renderer.render_legend(
        "viridis", rescale=(0.0, 1.0), width=60, height=256, orientation="vertical"
    )
    img = _decode(png)
    assert img.shape == (256, 60, 4)
    # Right strip should have text (dark pixels).
    label_strip = img[:, -20:, :3]
    dark_pixel_count = (label_strip.sum(axis=-1) < 200).sum()
    assert dark_pixel_count > 10


def test_colorbar_top_strip_varies_across_width():
    """A continuous colormap should show variation across the bar (not flat color)."""
    png = legend_renderer.render_legend("viridis", width=256, height=20)
    img = _decode(png)
    # First row across all columns — color should differ from left to right.
    left = img[0, 0, :3]
    right = img[0, -1, :3]
    assert tuple(left) != tuple(right), "color bar appears flat — gradient missing"


def test_legend_caches_results():
    """Hitting render_legend twice with the same args should return the SAME bytes."""
    a = legend_renderer.render_legend("viridis", rescale=(0.0, 1.0))
    b = legend_renderer.render_legend("viridis", rescale=(0.0, 1.0))
    assert a is b  # lru_cache returns the same object


def test_different_rescale_produces_different_legend():
    a = legend_renderer.render_legend("viridis", rescale=(0.0, 1.0), width=128, height=40)
    b = legend_renderer.render_legend("viridis", rescale=(0.0, 100.0), width=128, height=40)
    assert a != b, "different rescale should change tick labels — PNG bytes should differ"


def test_categorical_renders_discrete_blocks(monkeypatch):
    """Categorical colormaps should render as equal-width blocks, not a gradient."""
    import app.services.colormap.registry as colormap_config

    # Register a 3-category colormap inline.
    lut = [[0, 0, 0, 0] for _ in range(256)]
    lut[0] = [255, 0, 0, 255]
    lut[100] = [0, 255, 0, 255]
    lut[200] = [0, 0, 255, 255]

    monkeypatch.setitem(colormap_config._custom_colormaps, "_test_cats", lut)
    monkeypatch.setitem(colormap_config._custom_colormap_modes, "_test_cats", "categorical")

    # Force LRU caches to refresh against the just-registered colormap.
    import app.services.colormap.resolver as colormap_lookup

    colormap_lookup.resolve_colormap.cache_clear()

    png = legend_renderer.render_legend("_test_cats", width=300, height=20)
    img = _decode(png)
    assert img.shape == (20, 300, 4)
    # Across 300px split into 3 equal blocks (100px each), the first 50px should be red.
    assert img[10, 25, 0] == 255 and img[10, 25, 1] == 0


def test_categorical_legend_rejects_rescale(monkeypatch):
    """Tick labels at lo/mid/hi are meaningless for discrete categories — a categorical
    colormap with rescale is a contradiction, so reject it instead of drawing bogus ticks."""
    import app.services.colormap.registry as colormap_config

    lut = [[0, 0, 0, 0] for _ in range(256)]
    lut[0] = [255, 0, 0, 255]
    lut[100] = [0, 255, 0, 255]
    lut[200] = [0, 0, 255, 255]
    monkeypatch.setitem(colormap_config._custom_colormaps, "_test_cats", lut)
    monkeypatch.setitem(colormap_config._custom_colormap_modes, "_test_cats", "categorical")

    import app.services.colormap.resolver as colormap_lookup

    colormap_lookup.resolve_colormap.cache_clear()

    with pytest.raises(ValueError, match="(?i)rescale"):
        legend_renderer.render_legend("_test_cats", rescale=(0.0, 1.0), width=300, height=40)


def test_horizontal_height_too_small_for_labels_does_not_crash():
    """Edge case: height < _LABEL_PX (20) leaves bar_h=1 — should still render."""
    png = legend_renderer.render_legend("viridis", rescale=(0.0, 1.0), width=100, height=10)
    img = _decode(png)
    assert img.shape == (10, 100, 4)


def test_invalidation_hook_clears_cache():
    """colormap_config invalidation must drop legend caches."""
    _ = legend_renderer.render_legend("viridis", width=64, height=20)
    assert legend_renderer.render_legend.cache_info().currsize > 0

    # Trigger the invalidation chain.
    import app.services.colormap.registry as colormap_config

    for hook in colormap_config._invalidation_hooks:
        hook()

    assert legend_renderer.render_legend.cache_info().currsize == 0
