import json
import logging
from pathlib import Path
from typing import Literal

from constants import COLORMAPS_CONFIG_PATH

ColormapMode = Literal["ramp", "categorical"]

logger = logging.getLogger("services")

_config_path = Path(COLORMAPS_CONFIG_PATH)
_custom_colormaps: dict[str, list[tuple[int, int, int, int]]] = {}
_custom_colormap_modes: dict[str, ColormapMode] = {}


def get_colormap(name: str) -> list[tuple[int, int, int, int]] | None:
    """Return the 256-entry LUT for a custom colormap, or None if not registered."""
    return _custom_colormaps.get(name)


def is_categorical(name: str) -> bool:
    """Return True if the colormap was registered in categorical mode."""
    return _custom_colormap_modes.get(name) == "categorical"


def load_colormaps() -> None:
    """Read colormaps.json from disk into the in-memory registry. Called once on startup."""
    if not _config_path.exists():
        logger.info("No colormaps.json found — starting with in-memory defaults only")
        return
    data: dict[str, list | dict] = json.loads(_config_path.read_text())
    _reload(data)
    logger.info("Loaded %d colormap(s) from %s", len(_custom_colormaps), _config_path)


def register_colormap(
    name: str, entries: list[tuple[int, int, int, int]], mode: ColormapMode = "ramp"
) -> None:
    """Persist a new colormap. Raises ValueError if name already exists."""
    if name in _custom_colormaps:
        raise ValueError(f"Colormap '{name}' already exists — use PUT to update")
    data = _read_file()
    data[name] = entries if mode == "ramp" else {"entries": list(entries), "mode": mode}
    _write_file(data)
    _reload(data)


def remove_colormap(name: str) -> None:
    """Delete a colormap. Raises KeyError if name not found."""
    if name not in _custom_colormaps:
        raise KeyError(name)
    data = _read_file()
    data.pop(name, None)
    _write_file(data)
    _reload(data)


def list_colormaps() -> dict[str, list[str]]:
    """Return all supported colormap names grouped by source.

    Priority mirrors _colormap(): custom → rio-tiler → matplotlib.
    """
    import matplotlib
    from rio_tiler.colormap import cmap as _rio_cmap

    custom_names = list(_custom_colormaps.keys())
    custom_set = set(custom_names)

    rio_names = sorted(n for n in _rio_cmap.list() if n not in custom_set)
    rio_set = set(rio_names)

    mpl_names = sorted(n for n in matplotlib.colormaps if n not in custom_set and n not in rio_set)

    return {"custom": custom_names, "rio_tiler": rio_names, "matplotlib": mpl_names}


def _read_file() -> dict[str, list]:
    if not _config_path.exists():
        return {}
    return json.loads(_config_path.read_text())


def _write_file(data: dict[str, list]) -> None:
    _config_path.write_text(json.dumps(data, indent=2))


def _reload(data: dict[str, list | dict]) -> None:
    _custom_colormaps.clear()
    _custom_colormap_modes.clear()
    for name, value in data.items():
        if isinstance(value, dict):
            _custom_colormaps[name] = [tuple(rgba) for rgba in value["entries"]]  # type: ignore[misc]
            _custom_colormap_modes[name] = value["mode"]
        else:
            _custom_colormaps[name] = [tuple(rgba) for rgba in value]  # type: ignore[misc]
    from services.visual_renderer import _colormap

    _colormap.cache_clear()
