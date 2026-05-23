"""Colormap name → LUT resolution for the rendering pipeline.

Distinct from [[colormap.registry]]: that module handles persistence and the
custom registry on disk. This module is the runtime fallback chain used by
every render call — custom → rio-tiler → matplotlib — plus the LRU caches that
keep repeat lookups O(1).

LRU cache invalidation is wired here (rather than in colormap_config) to keep
the dependency direction one-way: colormap_config fires hooks; downstream
consumers register themselves.
"""

from functools import lru_cache

import numpy as np

from app.services.colormap.registry import get_colormap, on_invalidate


# LRU because resolve_colormap is called on every visual-tile render; render_legend
# has its own cache so this LRU is effectively just for the tile path.
@lru_cache(maxsize=64)
def resolve_colormap(name: str) -> dict[int, tuple[int, int, int, int]]:
    """Return a rio-tiler colormap dict for the given name.

    Checks custom colormaps first, then rio-tiler's built-ins, then matplotlib
    so that diverging colormaps like RdBu_r are also available.
    """
    from rio_tiler.colormap import cmap as _rio_cmap

    entries = get_colormap(name)
    if entries is not None:
        if len(entries) != 256:
            raise ValueError(
                f"Custom colormap {name!r} must have exactly 256 entries, got {len(entries)}"
            )
        return {i: entries[i] for i in range(256)}
    try:
        return _rio_cmap.get(name)
    except Exception:
        pass
    import matplotlib

    try:
        cm = matplotlib.colormaps[name]
    except KeyError as exc:
        raise ValueError(f"Unknown colormap: {name!r}") from exc
    rgba = (cm(np.linspace(0, 1, 256)) * 255).astype(np.uint8)
    return {
        i: (int(rgba[i, 0]), int(rgba[i, 1]), int(rgba[i, 2]), int(rgba[i, 3])) for i in range(256)
    }


on_invalidate(resolve_colormap.cache_clear)
