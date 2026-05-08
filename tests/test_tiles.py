from unittest.mock import patch

import numpy as np
import xarray as xr
from starlette.testclient import TestClient

from main import app

client = TestClient(app, raise_server_exceptions=True)

_LOD_GRIDS = {1: (1, 1)}


def _make_ds() -> xr.Dataset:
    lat = np.linspace(-40, -30, 8)
    lon = np.linspace(140, 150, 8)
    return xr.Dataset(
        {
            "GSLA": xr.DataArray(
                np.random.rand(8, 8), dims=["lat", "lon"], coords={"lat": lat, "lon": lon}
            )
        }
    )


# --- /tiles/{product}/{date}/{z}/{x}/{y}.png ---


def test_tile_unknown_product():
    response = client.get("/tiles/nonexistent/2024-01-01/1/0/0.png")
    assert response.status_code == 404


def test_tile_bad_lod():
    with (
        patch("routers.tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("routers.tiles.load_slice", return_value=_make_ds()),
    ):
        response = client.get("/tiles/sea_level_anomaly/2024-01-01/99/0/0.png")
    assert response.status_code == 404


def test_tile_out_of_bounds():
    with (
        patch("routers.tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("routers.tiles.load_slice", return_value=_make_ds()),
    ):
        response = client.get("/tiles/sea_level_anomaly/2024-01-01/1/5/5.png")
    assert response.status_code == 404


def test_tile_missing_date():
    with (
        patch("routers.tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("routers.tiles.load_slice", side_effect=FileNotFoundError("No data")),
    ):
        response = client.get("/tiles/sea_level_anomaly/9999-01-01/1/0/0.png")
    assert response.status_code == 404


def test_tile_ok():
    with (
        patch("routers.tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("routers.tiles.load_slice", return_value=_make_ds()),
        patch("routers.tiles.render_tile", return_value=b"\x89PNG\r\n\x1a\n"),
    ):
        response = client.get("/tiles/sea_level_anomaly/2024-01-01/1/0/0.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


# --- /tiles/{product}/{date}/manifest.json ---


def test_manifest_unknown_product():
    response = client.get("/tiles/nonexistent/2024-01-01/manifest.json")
    assert response.status_code == 404


def test_manifest_missing_date():
    with (
        patch("routers.tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("routers.tiles.load_slice", side_effect=FileNotFoundError("No data")),
    ):
        response = client.get("/tiles/sea_level_anomaly/9999-01-01/manifest.json")
    assert response.status_code == 404


def test_manifest_ok():
    payload = {"bounds": {}, "valueRange": [0.0, 1.0], "lods": {"1": {"grid": [1, 1]}}}
    with (
        patch("routers.tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("routers.tiles.load_slice", return_value=_make_ds()),
        patch("routers.tiles.render_manifest", return_value=payload),
    ):
        response = client.get("/tiles/sea_level_anomaly/2024-01-01/manifest.json")
    assert response.status_code == 200
    assert response.json() == payload


# --- /tiles/{product}/{date}/point ---


def test_point_unknown_product():
    response = client.get("/tiles/nonexistent/2024-01-01/point?lat=-35&lon=145")
    assert response.status_code == 404


def test_point_missing_date():
    with patch("routers.tiles.load_slice", side_effect=FileNotFoundError("No data")):
        response = client.get("/tiles/sea_level_anomaly/9999-01-01/point?lat=-35&lon=145")
    assert response.status_code == 404


def test_point_ok():
    with patch("routers.tiles.load_slice", return_value=_make_ds()):
        response = client.get("/tiles/sea_level_anomaly/2024-01-01/point?lat=-35&lon=145")
    assert response.status_code == 200
    body = response.json()
    assert "lat" in body and "lon" in body and "variables" in body
    assert "GSLA" in body["variables"]
