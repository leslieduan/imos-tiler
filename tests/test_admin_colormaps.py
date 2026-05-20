from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=True)

_ADMIN_KEY = "test-secret"
_HEADERS = {"X-Admin-Key": _ADMIN_KEY}


@pytest.fixture(autouse=True)
def admin_key_env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", _ADMIN_KEY)


# ---------------------------------------------------------------------------
# POST /admin/colormaps — valid payloads
# ---------------------------------------------------------------------------


def test_add_ramp_colormap_hex_stops():
    payload = {
        "name": "imos_sst",
        "mode": "ramp",
        "entries": ["#000080", "#00ffff", "#ffffff", "#ff8c00", "#8b0000"],
    }
    with patch("app.routers.admin.colormaps.register_colormap") as mock_reg:
        response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 201
    assert response.json() == {"name": "imos_sst"}
    mock_reg.assert_called_once()
    name, lut, mode = mock_reg.call_args.args
    assert name == "imos_sst"
    assert mode == "ramp"
    assert len(lut) == 256


def test_add_ramp_colormap_rgba_stops():
    payload = {
        "name": "imos_sst_rgba",
        "mode": "ramp",
        "entries": [[0, 0, 128, 255], [0, 255, 255, 255], [255, 255, 255, 255]],
    }
    with patch("app.routers.admin.colormaps.register_colormap") as mock_reg:
        response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 201
    mock_reg.assert_called_once()
    _, lut, mode = mock_reg.call_args.args
    assert mode == "ramp"
    assert len(lut) == 256


def test_add_ramp_colormap_mode_defaults_to_ramp():
    """Omitting mode is valid — it defaults to ramp."""
    payload = {
        "name": "implicit_ramp",
        "entries": ["#000000", "#ffffff"],
    }
    with patch("app.routers.admin.colormaps.register_colormap") as mock_reg:
        response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 201
    _, lut, mode = mock_reg.call_args.args
    assert mode == "ramp"
    assert len(lut) == 256


def test_add_categorical_colormap():
    payload = {
        "name": "land_cover",
        "mode": "categorical",
        "entries": {"1": "#ffff00", "2": "#0000ff", "3": "#ff0000", "4": "#000000"},
    }
    with patch("app.routers.admin.colormaps.register_colormap") as mock_reg:
        response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 201
    assert response.json() == {"name": "land_cover"}
    _, lut, mode = mock_reg.call_args.args
    assert mode == "categorical"
    assert len(lut) == 256


def test_add_categorical_colormap_rgba_values():
    payload = {
        "name": "land_cover_rgba",
        "mode": "categorical",
        "entries": {"1": [255, 255, 0, 255], "2": [0, 0, 255, 255]},
    }
    with patch("app.routers.admin.colormaps.register_colormap") as mock_reg:
        response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 201
    mock_reg.assert_called_once()


# ---------------------------------------------------------------------------
# POST /admin/colormaps — error cases
# ---------------------------------------------------------------------------


def test_add_colormap_duplicate_returns_409():
    payload = {
        "name": "existing",
        "mode": "ramp",
        "entries": ["#000000", "#ffffff"],
    }
    with patch(
        "app.routers.admin.colormaps.register_colormap", side_effect=ValueError("already exists")
    ):
        response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_add_colormap_too_few_stops():
    payload = {"name": "bad", "mode": "ramp", "entries": ["#000000"]}
    response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 422


def test_add_colormap_too_many_stops():
    payload = {"name": "bad", "mode": "ramp", "entries": [[i, 0, 0, 255] for i in range(257)]}
    response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 422


def test_add_colormap_invalid_hex_color():
    payload = {"name": "bad", "mode": "ramp", "entries": ["not-a-color", "#ffffff"]}
    response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 422


def test_add_colormap_invalid_rgba_values():
    payload = {"name": "bad", "mode": "ramp", "entries": [[300, 0, 0, 255], [0, 0, 0, 255]]}
    response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 422


def test_add_colormap_categorical_non_integer_key():
    payload = {
        "name": "bad",
        "mode": "categorical",
        "entries": {"not_an_int": "#ff0000", "2": "#0000ff"},
    }
    response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 422


def test_add_colormap_dict_entries_with_ramp_mode():
    """A dict passed for entries is only valid with categorical mode."""
    payload = {
        "name": "bad",
        "mode": "ramp",
        "entries": {"1": "#ff0000", "2": "#0000ff"},
    }
    response = client.post("/admin/colormaps", json=payload, headers=_HEADERS)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /admin/colormaps/{name}
# ---------------------------------------------------------------------------


def test_delete_colormap():
    with patch("app.routers.admin.colormaps.remove_colormap") as mock_rem:
        response = client.delete("/admin/colormaps/imos_sst", headers=_HEADERS)
    assert response.status_code == 204
    mock_rem.assert_called_once_with("imos_sst")


def test_delete_colormap_not_found():
    with patch("app.routers.admin.colormaps.remove_colormap", side_effect=KeyError("imos_sst")):
        response = client.delete("/admin/colormaps/imos_sst", headers=_HEADERS)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_missing_admin_key_returns_401():
    payload = {"name": "x", "mode": "ramp", "entries": ["#000000", "#ffffff"]}
    response = client.post("/admin/colormaps", json=payload)
    assert response.status_code == 401


def test_wrong_admin_key_returns_403():
    payload = {"name": "x", "mode": "ramp", "entries": ["#000000", "#ffffff"]}
    response = client.post("/admin/colormaps", json=payload, headers={"X-Admin-Key": "wrong"})
    assert response.status_code == 403
