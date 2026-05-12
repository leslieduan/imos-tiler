from unittest.mock import patch

import numpy as np
import xarray as xr
from starlette.testclient import TestClient

from main import app

client = TestClient(app, raise_server_exceptions=True)

_PNG = b"\x89PNG\r\n\x1a\n"


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


def test_tile_unknown_product():
    response = client.get("/visual_tiles/nonexistent/2024-01-01/5/0/0.png")
    assert response.status_code == 404


def test_tile_multi_variable_product_rejected():
    response = client.get("/visual_tiles/ocean_current/2024-01-01/5/0/0.png")
    assert response.status_code == 400


def test_tile_missing_date():
    with patch("routers.visual_tiles.load_slice", side_effect=FileNotFoundError("No data")):
        response = client.get("/visual_tiles/sea_level_anomaly/9999-01-01/5/0/0.png")
    assert response.status_code == 404


def test_tile_bad_rescale():
    with patch("routers.visual_tiles.load_slice", return_value=_make_ds()):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?rescale=bad")
    assert response.status_code == 400


def test_tile_unknown_colormap():
    with patch("routers.visual_tiles.load_slice", return_value=_make_ds()):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?colormap=not_a_real_colormap"
        )
    assert response.status_code == 400


def test_tile_ok():
    with (
        patch("routers.visual_tiles.load_slice", return_value=_make_ds()),
        patch("routers.visual_tiles.render_tile", return_value=_PNG),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_tile_ok_with_rescale():
    with (
        patch("routers.visual_tiles.load_slice", return_value=_make_ds()),
        patch("routers.visual_tiles.render_tile", return_value=_PNG),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?rescale=-0.5,0.5"
        )
    assert response.status_code == 200


def test_tile_ok_with_custom_colormap():
    custom = [(i, 0, 255 - i, 255) for i in range(256)]
    with (
        patch("routers.visual_tiles.load_slice", return_value=_make_ds()),
        patch("routers.visual_tiles.render_tile", return_value=_PNG),
        patch("services.visual_renderer.CUSTOM_COLORMAPS", {"test_ramp": custom}),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?colormap=test_ramp"
        )
    assert response.status_code == 200
