import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from app.config.paths import COLORMAPS_CONFIG_PATH

ColormapMode = Literal["ramp", "categorical"]

_config_path = Path(COLORMAPS_CONFIG_PATH)
_custom_colormaps: dict[str, list[tuple[int, int, int, int]]] = {}
_custom_colormap_modes: dict[str, ColormapMode] = {}
# Category values (sorted) for categorical colormaps. The 256-LUT alone can't
# recover them — transparent categories look identical to unmapped slots — so we
# store them explicitly to validate a colormap against a product's flag_values at
# request time (see [[colormap.categorical]] callers).
_custom_colormap_values: dict[str, list[int]] = {}

# Callbacks invoked whenever the registry changes. Lets downstream modules
# (e.g. colormap_lookup, legend_renderer) clear their LUT/legend LRU caches
# without us importing them — colormap_config would otherwise have to do a
# function-local import of those modules to break the cycle. The list is
# populated at downstream-module import time and never trimmed; the small
# bounded set is fine for this app.
_invalidation_hooks: list[Callable[[], None]] = []


def on_invalidate(hook: Callable[[], None]) -> None:
    """Register a callback to fire after every colormap registry change."""
    _invalidation_hooks.append(hook)


def get_colormap(name: str) -> list[tuple[int, int, int, int]] | None:
    """Return the 256-entry LUT for a custom colormap, or None if not registered."""
    return _custom_colormaps.get(name)


def is_categorical(name: str) -> bool:
    """Return True if the colormap was registered in categorical mode."""
    return _custom_colormap_modes.get(name) == "categorical"


def get_category_values(name: str) -> list[int] | None:
    """Return the sorted category values of a categorical colormap, or None.

    None means the name is unknown or was not registered as categorical.
    """
    return _custom_colormap_values.get(name)


def load_colormaps() -> None:
    """Read colormaps.json from disk into the in-memory registry. Called once on startup."""
    if not _config_path.exists():
        print("No colormaps.json found — starting with in-memory defaults only")
        return
    data: dict[str, list | dict] = json.loads(_config_path.read_text())
    _reload(data)
    print(f"Loaded {len(_custom_colormaps)} colormaps from {_config_path}")


def list_colormaps() -> dict[str, list]:
    """Return all supported colormap names grouped by source.

    Priority mirrors _colormap(): custom → rio-tiler → matplotlib.
    Custom entries include their mode; rio-tiler and matplotlib entries are plain strings.
    """
    import matplotlib
    from rio_tiler.colormap import cmap as _rio_cmap

    custom = [
        {"name": name, "mode": _custom_colormap_modes.get(name, "ramp")}
        for name in _custom_colormaps
    ]
    custom_set = {entry["name"] for entry in custom}

    rio_names = sorted(n for n in _rio_cmap.list() if n not in custom_set)
    rio_set = set(rio_names)

    mpl_names = sorted(n for n in matplotlib.colormaps if n not in custom_set and n not in rio_set)

    return {"custom": custom, "rio_tiler": rio_names, "matplotlib": mpl_names}


def _reload(data: dict[str, list | dict]) -> None:
    _custom_colormaps.clear()
    _custom_colormap_modes.clear()
    _custom_colormap_values.clear()
    for name, value in data.items():
        if isinstance(value, dict):
            _custom_colormaps[name] = [tuple(rgba) for rgba in value["entries"]]  # type: ignore[misc]
            _custom_colormap_modes[name] = value["mode"]
            _custom_colormap_values[name] = list(value.get("values", []))
        else:
            _custom_colormaps[name] = [tuple(rgba) for rgba in value]  # type: ignore[misc]
    for hook in _invalidation_hooks:
        hook()
