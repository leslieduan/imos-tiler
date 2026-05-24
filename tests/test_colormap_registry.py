"""colormap_config: JSON round-trip, missing file, registry mutations.

These tests bypass the registration HTTP layer entirely so we can prove the
persistence module is self-consistent: writes survive a reload, deletions
remove entries, malformed JSON surfaces clearly, and invalidation hooks fire.
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
    )
    colormap_config._custom_colormaps.clear()
    colormap_config._custom_colormap_modes.clear()
    yield cfg
    colormap_config._custom_colormaps.clear()
    colormap_config._custom_colormap_modes.clear()
    colormap_config._custom_colormaps.update(saved[0])
    colormap_config._custom_colormap_modes.update(saved[1])


def _entries():
    """A trivial 2-stop LUT — content doesn't matter, only round-trip."""
    return [[i, 0, 0, 255] for i in range(256)]


def test_load_colormaps_no_file_is_noop(isolated_config):
    """Missing config file must not crash startup."""
    assert not isolated_config.exists()
    colormap_config.load_colormaps()
    assert colormap_config._custom_colormaps == {}


def test_register_then_load_round_trip(isolated_config):
    colormap_config.register_colormap("my_ramp", _entries(), mode="ramp")
    # Persisted to disk.
    on_disk = json.loads(isolated_config.read_text())
    assert "my_ramp" in on_disk
    # In-memory registry has it.
    assert colormap_config.get_colormap("my_ramp") is not None
    assert not colormap_config.is_categorical("my_ramp")

    # Clear in-memory + reload — should restore.
    colormap_config._custom_colormaps.clear()
    colormap_config._custom_colormap_modes.clear()
    colormap_config.load_colormaps()
    assert colormap_config.get_colormap("my_ramp") is not None
    # Returned entries should be tuples (cast in _reload).
    assert isinstance(colormap_config.get_colormap("my_ramp")[0], tuple)


def test_register_categorical_persists_mode(isolated_config):
    colormap_config.register_colormap("cats", _entries(), mode="categorical")
    on_disk = json.loads(isolated_config.read_text())
    # Categorical stored as dict with explicit mode field.
    assert on_disk["cats"]["mode"] == "categorical"
    assert colormap_config.is_categorical("cats")


def test_register_duplicate_raises(isolated_config):
    colormap_config.register_colormap("dup", _entries())
    with pytest.raises(ValueError, match="already exists"):
        colormap_config.register_colormap("dup", _entries())


def test_remove_colormap_removes_from_disk_and_memory(isolated_config):
    colormap_config.register_colormap("to_delete", _entries())
    assert colormap_config.get_colormap("to_delete") is not None

    colormap_config.remove_colormap("to_delete")

    assert colormap_config.get_colormap("to_delete") is None
    on_disk = json.loads(isolated_config.read_text())
    assert "to_delete" not in on_disk


def test_remove_unknown_raises_keyerror(isolated_config):
    with pytest.raises(KeyError):
        colormap_config.remove_colormap("never_registered")


def test_invalidation_hook_fires_on_register(isolated_config):
    fired: list[int] = []
    colormap_config.on_invalidate(lambda: fired.append(1))
    colormap_config.register_colormap("a", _entries())
    assert fired == [1]
    colormap_config.remove_colormap("a")
    assert fired == [1, 1]


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
    colormap_config.register_colormap("zzz_my_custom", _entries(), mode="ramp")
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


def test_write_uses_indented_json(isolated_config):
    """File must be human-readable so ops can grep/diff it."""
    colormap_config.register_colormap("readable", _entries())
    raw = isolated_config.read_text()
    assert "\n" in raw, "indent=2 missing — file is one-line and hard to diff"


def test_path_is_pathlib_path(isolated_config):
    assert isinstance(colormap_config._config_path, Path)
