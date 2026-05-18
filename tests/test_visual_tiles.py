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
    with patch("routers.shared.load_slice", side_effect=FileNotFoundError("No data")):
        response = client.get("/visual_tiles/sea_level_anomaly/9999-01-01/5/0/0.png")
    assert response.status_code == 404


def test_tile_bad_rescale():
    with patch("routers.shared.load_slice", return_value=_make_ds()):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?rescale=bad")
    assert response.status_code == 400


def test_tile_unknown_colormap():
    with patch("routers.shared.load_slice", return_value=_make_ds()):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?colormap=not_a_real_colormap"
        )
    assert response.status_code == 400


def test_tile_ok():
    with (
        patch("routers.shared.load_slice", return_value=_make_ds()),
        patch("routers.visual_tiles.render_tile", return_value=_PNG),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_tile_ok_with_rescale():
    with (
        patch("routers.shared.load_slice", return_value=_make_ds()),
        patch("routers.visual_tiles.render_tile", return_value=_PNG),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?rescale=-0.5,0.5"
        )
    assert response.status_code == 200


def test_tile_ok_with_custom_colormap():
    custom = [(i, 0, 255 - i, 255) for i in range(256)]
    with (
        patch("routers.shared.load_slice", return_value=_make_ds()),
        patch("routers.visual_tiles.render_tile", return_value=_PNG),
        patch("services.colormap_config._custom_colormaps", {"test_ramp": custom}),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?colormap=test_ramp"
        )
    assert response.status_code == 200


_WEBP = b"RIFF\x00\x00\x00\x00WEBP"


def test_tile_webp_ok():
    with (
        patch("routers.shared.load_slice", return_value=_make_ds()),
        patch("routers.visual_tiles.render_tile", return_value=_WEBP),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.webp")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"


def test_tile_unknown_extension_rejected():
    response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.jpg")
    assert response.status_code == 422


def test_tile_webp_rejected_for_categorical_colormap():
    categorical = [(0, 0, 0, 0)] * 256
    with (
        patch("routers.shared.load_slice", return_value=_make_ds()),
        patch("services.colormap_config._custom_colormaps", {"cat_map": categorical}),
        patch("services.colormap_config._custom_colormap_modes", {"cat_map": "categorical"}),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.webp?colormap=cat_map&rescale=1,4"
        )
    assert response.status_code == 400
    assert "categorical" in response.json()["detail"].lower()


def test_bbox_png_ok():
    with (
        patch("routers.shared.load_slice", return_value=_make_ds()),
        patch("routers.visual_tiles.render_bbox", return_value=_PNG),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/bbox.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_bbox_webp_ok():
    with (
        patch("routers.shared.load_slice", return_value=_make_ds()),
        patch("routers.visual_tiles.render_bbox", return_value=_WEBP),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/bbox.webp")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"


def test_bbox_legacy_url_without_extension_returns_404():
    response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/bbox")
    assert response.status_code == 404


_APNG = b"\x89PNG\r\n\x1a\n"


def test_animation_ok_with_default_bbox():
    with (
        patch(
            "routers.visual_tiles.get_available_dates",
            return_value=["2024-01-01", "2024-01-02", "2024-01-03"],
        ),
        patch("routers.visual_tiles.load_slice_uncached", return_value=_make_ds()),
        patch("routers.visual_tiles.render_bbox_animation", return_value=_APNG),
        patch(
            "routers.visual_tiles._default_bbox_from_store",
            return_value=(140.0, -40.0, 150.0, -30.0),
        ),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-03/animation.apng"
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/apng"
    # No cache header: animation is rare and we don't want CDN/browser holding it.
    assert "cache-control" not in {k.lower() for k in response.headers}


def test_animation_swapped_dates_rejected():
    response = client.get("/visual_tiles/sea_level_anomaly/2024-02-01/2024-01-01/animation.gif")
    assert response.status_code == 400


def test_animation_no_data_in_range_returns_404():
    with (
        patch("routers.visual_tiles.get_available_dates", return_value=["2025-01-01"]),
        patch(
            "routers.visual_tiles._default_bbox_from_store",
            return_value=(140.0, -40.0, 150.0, -30.0),
        ),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-31/animation.gif")
    assert response.status_code == 404


def test_animation_frame_cap_rejected():
    # 61 dates → one past the 60-frame cap.
    too_many = [f"2024-01-{d:02d}" for d in range(1, 32)] + [
        f"2024-02-{d:02d}" for d in range(1, 31)
    ]
    assert len(too_many) == 61
    with (
        patch("routers.visual_tiles.get_available_dates", return_value=too_many),
        patch(
            "routers.visual_tiles._default_bbox_from_store",
            return_value=(140.0, -40.0, 150.0, -30.0),
        ),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/2024-02-30/animation.gif")
    assert response.status_code == 400
    assert "max is 60" in response.json()["detail"]


def test_animation_multi_variable_product_rejected():
    response = client.get("/visual_tiles/ocean_current/2024-01-01/2024-01-02/animation.gif")
    assert response.status_code == 400


def test_animation_unknown_format_rejected():
    response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-02/animation.jpg")
    assert response.status_code == 422


def test_animation_explicit_bbox_passed_through():
    captured = {}

    def fake_render(*args, **_kwargs):
        captured["bbox"] = args[2]  # render_bbox_animation(datasets, variable, bbox, ...)
        return _APNG

    with (
        patch("routers.visual_tiles.get_available_dates", return_value=["2024-01-01"]),
        patch("routers.visual_tiles.load_slice_uncached", return_value=_make_ds()),
        patch("routers.visual_tiles.render_bbox_animation", side_effect=fake_render),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-01/animation.apng"
            "?bbox=100,-50,160,-10"
        )
    assert response.status_code == 200
    assert captured["bbox"] == (100.0, -50.0, 160.0, -10.0)
