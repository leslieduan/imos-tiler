from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from starlette.testclient import TestClient

from app.main import app

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
    with patch("app.routers.shared.load_slice", side_effect=FileNotFoundError("No data")):
        response = client.get("/visual_tiles/sea_level_anomaly/9999-01-01/5/0/0.png")
    assert response.status_code == 404


def test_tile_bad_rescale():
    with patch("app.routers.shared.load_slice", return_value=_make_ds()):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?rescale=bad")
    assert response.status_code == 400


def test_tile_unknown_colormap():
    with patch("app.routers.shared.load_slice", return_value=_make_ds()):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?colormap=not_a_real_colormap"
        )
    assert response.status_code == 400


def test_tile_ok():
    with (
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.public.visual_tiles.render_tile", return_value=_PNG),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_tile_ok_with_rescale():
    with (
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.public.visual_tiles.render_tile", return_value=_PNG),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?rescale=-0.5,0.5"
        )
    assert response.status_code == 200


def test_tile_ok_with_custom_colormap():
    custom = [(i, 0, 255 - i, 255) for i in range(256)]
    with (
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.public.visual_tiles.render_tile", return_value=_PNG),
        patch("app.services.colormap.registry._custom_colormaps", {"test_ramp": custom}),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png?colormap=test_ramp"
        )
    assert response.status_code == 200


_WEBP = b"RIFF\x00\x00\x00\x00WEBP"


def test_tile_webp_ok():
    with (
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.public.visual_tiles.render_tile", return_value=_WEBP),
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
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.services.colormap.registry._custom_colormaps", {"cat_map": categorical}),
        patch("app.services.colormap.registry._custom_colormap_modes", {"cat_map": "categorical"}),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.webp?colormap=cat_map&rescale=1,4"
        )
    assert response.status_code == 400
    assert "categorical" in response.json()["detail"].lower()


def test_bbox_png_ok():
    with (
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.public.visual_tiles.render_bbox", return_value=_PNG),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/bbox.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_bbox_webp_ok():
    with (
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.public.visual_tiles.render_bbox", return_value=_WEBP),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/bbox.webp")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"


def test_bbox_legacy_url_without_extension_returns_404():
    response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/bbox")
    assert response.status_code == 404


_APNG = b"\x89PNG\r\n\x1a\n"


def _make_categorical_ds() -> xr.Dataset:
    lat = np.linspace(-40, -30, 8)
    lon = np.linspace(140, 150, 8)
    data = (np.arange(64) % 5).reshape(8, 8).astype("float32")
    da = xr.DataArray(data, dims=["lat", "lon"], coords={"lat": lat, "lon": lon})
    da.attrs["flag_values"] = [0, 1, 2, 3, 4]
    da.attrs["flag_meanings"] = "none moderate strong severe extreme"
    return xr.Dataset({"MCS_category": da})


@pytest.fixture
def mcs_product():
    """Register a categorical product for the duration of one test.

    Kept out of the global conftest seed so tests that assert the exact seeded
    product set stay valid.
    """
    from app.services.product.product import Product
    from app.services.product.registry import PRODUCTS

    PRODUCTS["mcs"] = Product(
        id="mcs",
        source_path="s3://test/mcs.zarr",
        variable="MCS_category",
    )
    yield
    PRODUCTS.pop("mcs", None)


def test_categorical_tile_ok(mcs_product):
    # render_tile is NOT mocked here — exercise the real discrete-lookup path.
    with patch("app.routers.shared.load_slice", return_value=_make_categorical_ds()):
        response = client.get("/visual_tiles/mcs/2024-01-01/0/0/0.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_categorical_tile_rejects_webp(mcs_product):
    with patch("app.routers.shared.load_slice", return_value=_make_categorical_ds()):
        response = client.get("/visual_tiles/mcs/2024-01-01/0/0/0.webp")
    assert response.status_code == 400
    assert "webp" in response.json()["detail"].lower()


def test_categorical_tile_rejects_continuous_colormap(mcs_product):
    with patch("app.routers.shared.load_slice", return_value=_make_categorical_ds()):
        response = client.get("/visual_tiles/mcs/2024-01-01/0/0/0.png?colormap=plasma")
    assert response.status_code == 400
    assert "categorical" in response.json()["detail"].lower()


def test_categorical_legend_json(mcs_product):
    with patch("app.routers.public.visual_tiles.get_store", return_value=_make_categorical_ds()):
        response = client.get("/visual_tiles/mcs/legend")
    assert response.status_code == 200
    body = response.json()
    assert body["categorical"] is True
    assert body["variable"] == "MCS_category"
    cats = body["categories"]
    assert [c["value"] for c in cats] == [0, 1, 2, 3, 4]
    assert cats[1]["label"] == "moderate"
    assert cats[1]["color"] == [199, 236, 242, 255]
    assert cats[0]["color"] == [0, 0, 0, 0]  # none transparent


def test_legend_json_rejected_for_continuous_product():
    with patch("app.routers.public.visual_tiles.get_store", return_value=_make_ds()):
        response = client.get("/visual_tiles/sea_level_anomaly/legend")
    assert response.status_code == 400
    assert "not categorical" in response.json()["detail"].lower()


def test_categorical_legend_png(mcs_product):
    with patch("app.routers.public.visual_tiles.get_store", return_value=_make_categorical_ds()):
        response = client.get("/visual_tiles/mcs/legend.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == _PNG


def test_animation_ok_with_default_bbox():
    with (
        patch(
            "app.routers.public.visual_tiles.get_available_dates",
            return_value=["2024-01-01", "2024-01-02", "2024-01-03"],
        ),
        patch("app.routers.public.visual_tiles.load_slice_uncached", return_value=_make_ds()),
        patch("app.routers.public.visual_tiles.render_bbox_animation", return_value=_APNG),
        patch(
            "app.routers.public.visual_tiles.default_bbox_from_store",
            return_value=(140.0, -40.0, 150.0, -30.0),
        ),
        patch(
            "app.routers.public.visual_tiles.native_resolution_in_bbox",
            return_value=(256, 256),
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
        patch("app.routers.public.visual_tiles.get_available_dates", return_value=["2025-01-01"]),
        patch(
            "app.routers.public.visual_tiles.default_bbox_from_store",
            return_value=(140.0, -40.0, 150.0, -30.0),
        ),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-31/animation.gif")
    assert response.status_code == 404


def test_animation_frame_cap_rejected():
    # 31 dates → one past the 30-frame cap.
    too_many = [f"2024-01-{d:02d}" for d in range(1, 32)]
    assert len(too_many) == 31
    with (
        patch("app.routers.public.visual_tiles.get_available_dates", return_value=too_many),
        patch(
            "app.routers.public.visual_tiles.default_bbox_from_store",
            return_value=(140.0, -40.0, 150.0, -30.0),
        ),
    ):
        response = client.get("/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-31/animation.gif")
    assert response.status_code == 400
    assert "max is 30" in response.json()["detail"]


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
        patch("app.routers.public.visual_tiles.get_available_dates", return_value=["2024-01-01"]),
        patch("app.routers.public.visual_tiles.load_slice_uncached", return_value=_make_ds()),
        patch("app.routers.public.visual_tiles.render_bbox_animation", side_effect=fake_render),
    ):
        # width+height pinned so the test doesn't trip the native-resolution code path
        # (which would try to open the real store to read lat/lon spacing).
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-01/animation.apng"
            "?bbox=100,-50,160,-10&width=256&height=256"
        )
    assert response.status_code == 200
    assert captured["bbox"] == (100.0, -50.0, 160.0, -10.0)


def _capture_render_dims():
    """Patch render_bbox_animation to record the (width, height) the router passed.

    Returns (patch_obj, captured_dict). The dict gains 'wh' once render is called.
    """
    captured: dict = {}

    def fake_render(*args, **_kwargs):
        # render_bbox_animation(datasets, variable, bbox, width, height, ...)
        captured["wh"] = (args[3], args[4])
        return _APNG

    return patch(
        "app.routers.public.visual_tiles.render_bbox_animation", side_effect=fake_render
    ), captured


def test_animation_native_resolution_used_when_both_dims_omitted():
    patch_render, captured = _capture_render_dims()
    with (
        patch("app.routers.public.visual_tiles.get_available_dates", return_value=["2024-01-01"]),
        patch("app.routers.public.visual_tiles.load_slice_uncached", return_value=_make_ds()),
        patch("app.routers.public.visual_tiles.native_resolution_in_bbox", return_value=(640, 350)),
        patch_render,
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-01/animation.apng?bbox=57,-70,180,0"
        )
    assert response.status_code == 200
    assert captured["wh"] == (640, 350)


def test_animation_height_derived_from_bbox_aspect_when_only_width_given():
    # Bbox aspect: (150 - 100) / (-10 - -50) = 50 / 40 = 1.25
    # width=500 → derived height = round(500 / 1.25) = 400
    patch_render, captured = _capture_render_dims()
    with (
        patch("app.routers.public.visual_tiles.get_available_dates", return_value=["2024-01-01"]),
        patch("app.routers.public.visual_tiles.load_slice_uncached", return_value=_make_ds()),
        patch_render,
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-01/animation.apng"
            "?bbox=100,-50,150,-10&width=500"
        )
    assert response.status_code == 200
    assert captured["wh"] == (500, 400)


def test_animation_width_derived_from_bbox_aspect_when_only_height_given():
    # Bbox aspect: 50 / 40 = 1.25. height=400 → derived width = round(400 * 1.25) = 500.
    patch_render, captured = _capture_render_dims()
    with (
        patch("app.routers.public.visual_tiles.get_available_dates", return_value=["2024-01-01"]),
        patch("app.routers.public.visual_tiles.load_slice_uncached", return_value=_make_ds()),
        patch_render,
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-01/animation.apng"
            "?bbox=100,-50,150,-10&height=400"
        )
    assert response.status_code == 200
    assert captured["wh"] == (500, 400)


def test_animation_derived_dimension_clamped_to_max():
    # Extreme aspect ratio: bbox 100° wide × 0.5° tall → aspect 200.
    # width=2000 → derived height = round(2000 / 200) = 10 (well under cap, no clamp).
    # height=2000 → derived width = round(2000 * 200) = 400000 → clamped to 2048.
    patch_render, captured = _capture_render_dims()
    with (
        patch("app.routers.public.visual_tiles.get_available_dates", return_value=["2024-01-01"]),
        patch("app.routers.public.visual_tiles.load_slice_uncached", return_value=_make_ds()),
        patch_render,
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/2024-01-01/animation.apng"
            "?bbox=0,-0.25,100,0.25&height=2000"
        )
    assert response.status_code == 200
    assert captured["wh"] == (2048, 2000)
