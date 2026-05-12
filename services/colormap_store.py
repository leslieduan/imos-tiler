import json
import logging
from pathlib import Path

from constants import COLORMAPS_CONFIG_PATH, CUSTOM_COLORMAPS

logger = logging.getLogger("services")

_config_path = Path(COLORMAPS_CONFIG_PATH)


def load_colormaps() -> None:
    """Read colormaps.json from disk into CUSTOM_COLORMAPS. Called once on startup."""
    if not _config_path.exists():
        logger.info("No colormaps.json found — starting with in-memory defaults only")
        return
    data: dict[str, list] = json.loads(_config_path.read_text())
    CUSTOM_COLORMAPS.clear()
    for name, entries in data.items():
        CUSTOM_COLORMAPS[name] = [tuple(rgba) for rgba in entries]  # type: ignore[misc]
    logger.info("Loaded %d colormap(s) from %s", len(CUSTOM_COLORMAPS), _config_path)


def register_colormap(name: str, entries: list[tuple[int, int, int, int]]) -> None:
    """Persist a new colormap. Raises ValueError if name already exists."""
    if name in CUSTOM_COLORMAPS:
        raise ValueError(f"Colormap '{name}' already exists — use PUT to update")
    data = _read_file()
    data[name] = entries
    _write_file(data)
    _reload(data)


def remove_colormap(name: str) -> None:
    """Delete a colormap. Raises KeyError if name not found."""
    if name not in CUSTOM_COLORMAPS:
        raise KeyError(name)
    data = _read_file()
    data.pop(name, None)
    _write_file(data)
    _reload(data)


def list_colormaps() -> dict[str, list]:
    return _read_file()


def _read_file() -> dict[str, list]:
    if not _config_path.exists():
        return {}
    return json.loads(_config_path.read_text())


def _write_file(data: dict[str, list]) -> None:
    _config_path.write_text(json.dumps(data, indent=2))


def _reload(data: dict[str, list]) -> None:
    CUSTOM_COLORMAPS.clear()
    for name, entries in data.items():
        CUSTOM_COLORMAPS[name] = [tuple(rgba) for rgba in entries]  # type: ignore[misc]
    from services.visual_renderer import _colormap

    _colormap.cache_clear()
