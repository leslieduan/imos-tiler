"""colormap_config: JSON load, missing file, registry behavior.

Colormaps are static config now (no admin API), so these tests write
colormaps.json directly and prove load is self-consistent: files round-trip
through load, malformed JSON surfaces clearly, and invalidation hooks fire.
"""

import json
from pathlib import Path

import pytest

import app.services.colormap.registry as colormap_config


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point colormap_config at a tmp file and start with an empty registry."""
    cfg = tmp_path / "colormaps.json"
    monkeypatch.setattr(colormap_config, "_config_path", cfg)
    # Snapshot + clear in-memory state.
    saved = (
        dict(colormap_config._custom_colormaps),
        dict(colormap_config._custom_colormap_modes),
        dict(colormap_config._custom_colormap_values),
    )
    colormap_config._custom_colormaps.clear()
    colormap_config._custom_colormap_modes.clear()
    colormap_config._custom_colormap_values.clear()
    yield cfg
    colormap_config._custom_colormaps.clear()
    colormap_config._custom_colormap_modes.clear()
    colormap_config._custom_colormap_values.clear()
    colormap_config._custom_colormaps.update(saved[0])
    colormap_config._custom_colormap_modes.update(saved[1])
    colormap_config._custom_colormap_values.update(saved[2])


def _entries():
    """A trivial 2-stop LUT — content doesn't matter, only round-trip."""
    return [[i, 0, 0, 255] for i in range(256)]


def test_load_colormaps_no_file_is_noop(isolated_config):
    """Missing config file must not crash startup."""
    assert not isolated_config.exists()
    colormap_config.load_colormaps()
    assert colormap_config._custom_colormaps == {}


def test_load_ramp_from_disk(isolated_config):
    isolated_config.write_text(json.dumps({"my_ramp": _entries()}))
    colormap_config.load_colormaps()
    assert colormap_config.get_colormap("my_ramp") is not None
    assert not colormap_config.is_categorical("my_ramp")
    # Returned entries should be tuples (cast in _reload).
    assert isinstance(colormap_config.get_colormap("my_ramp")[0], tuple)


def test_load_categorical_persists_mode_and_values(isolated_config):
    isolated_config.write_text(
        json.dumps({"cats": {"entries": _entries(), "mode": "categorical", "values": [1, 2, 3]}})
    )
    colormap_config.load_colormaps()
    assert colormap_config.is_categorical("cats")
    assert colormap_config.get_category_values("cats") == [1, 2, 3]


def test_get_category_values_none_for_ramp(isolated_config):
    isolated_config.write_text(json.dumps({"ramp": _entries()}))
    colormap_config.load_colormaps()
    assert colormap_config.get_category_values("ramp") is None
    assert colormap_config.get_category_values("never_registered") is None


def test_invalidation_hook_fires_on_load(isolated_config):
    isolated_config.write_text(json.dumps({"x": _entries()}))
    fired: list[int] = []
    colormap_config.on_invalidate(lambda: fired.append(1))
    colormap_config.load_colormaps()
    assert fired == [1]


def test_load_handles_legacy_list_format(isolated_config):
    """Pre-mode format: bare list entries default to ramp mode."""
    isolated_config.write_text(json.dumps({"legacy": _entries()}))
    colormap_config.load_colormaps()
    assert colormap_config.get_colormap("legacy") is not None
    assert not colormap_config.is_categorical("legacy")


def test_load_handles_new_dict_format(isolated_config):
    isolated_config.write_text(
        json.dumps({"newcat": {"entries": _entries(), "mode": "categorical"}})
    )
    colormap_config.load_colormaps()
    assert colormap_config.is_categorical("newcat")


def test_malformed_json_raises_on_load(isolated_config):
    """Garbage in the config file should surface a JSON error, not be swallowed."""
    isolated_config.write_text("{not valid json")
    with pytest.raises(json.JSONDecodeError):
        colormap_config.load_colormaps()


def test_list_colormaps_includes_custom_first(isolated_config):
    isolated_config.write_text(json.dumps({"zzz_my_custom": _entries()}))
    colormap_config.load_colormaps()
    result = colormap_config.list_colormaps()
    assert any(e["name"] == "zzz_my_custom" for e in result["custom"])
    # Built-in lists exist.
    assert isinstance(result["rio_tiler"], list)
    assert isinstance(result["matplotlib"], list)
    # Custom name must NOT appear in rio_tiler/matplotlib (dedup'd).
    assert "zzz_my_custom" not in result["rio_tiler"]
    assert "zzz_my_custom" not in result["matplotlib"]


def test_get_colormap_returns_none_for_unknown(isolated_config):
    assert colormap_config.get_colormap("does_not_exist") is None


def test_path_is_pathlib_path(isolated_config):
    assert isinstance(colormap_config._config_path, Path)
