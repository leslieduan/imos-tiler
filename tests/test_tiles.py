from unittest.mock import patch

import numpy as np
import xarray as xr
from starlette.testclient import TestClient

from app.constants import Product
from app.main import app

client = TestClient(app, raise_server_exceptions=True)

_FAKE_PRODUCTS = {
    "product_a": Product(id="product_a", source_path="s3://bucket/a.zarr", variable="VAR"),
}

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


# --- /{product}/{date}/{z}/{x}/{y}.png ---


def test_tile_unknown_product():
    response = client.get("/data_tiles/nonexistent/2024-01-01/1/0/0.png")
    assert response.status_code == 404


def test_tile_bad_lod():
    with (
        patch("app.routers.data_tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
    ):
        response = client.get("/data_tiles/sea_level_anomaly/2024-01-01/99/0/0.png")
    assert response.status_code == 404


def test_tile_out_of_bounds():
    with (
        patch("app.routers.data_tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
    ):
        response = client.get("/data_tiles/sea_level_anomaly/2024-01-01/1/5/5.png")
    assert response.status_code == 404


def test_tile_missing_date():
    def _lod_grids_with_update(product):
        product.lod_grids.update(_LOD_GRIDS)
        return _LOD_GRIDS

    with (
        patch("app.routers.data_tiles.get_lod_grids", side_effect=_lod_grids_with_update),
        patch("app.routers.shared.load_slice", side_effect=FileNotFoundError("No data")),
    ):
        response = client.get("/data_tiles/sea_level_anomaly/9999-01-01/1/0/0.png")
    assert response.status_code == 404


def test_tile_ok():
    with (
        patch("app.routers.data_tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.data_tiles.render_tile", return_value=b"\x89PNG\r\n\x1a\n"),
    ):
        response = client.get("/data_tiles/sea_level_anomaly/2024-01-01/1/0/0.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


# --- /{product}/{date}/manifest.json ---


def test_manifest_unknown_product():
    response = client.get("/data_tiles/nonexistent/2024-01-01/manifest.json")
    assert response.status_code == 404


def test_manifest_missing_date():
    with (
        patch("app.routers.data_tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("app.routers.shared.load_slice", side_effect=FileNotFoundError("No data")),
    ):
        response = client.get("/data_tiles/sea_level_anomaly/9999-01-01/manifest.json")
    assert response.status_code == 404


def test_manifest_ok():
    payload = {"bounds": {}, "valueRange": [0.0, 1.0], "lods": {"1": {"grid": [1, 1]}}}
    with (
        patch("app.routers.data_tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.data_tiles.render_manifest", return_value=payload),
    ):
        response = client.get("/data_tiles/sea_level_anomaly/2024-01-01/manifest.json")
    assert response.status_code == 200
    assert response.json() == payload


# --- /{product}/{date}/point ---


def test_point_unknown_product():
    response = client.get("/data_tiles/nonexistent/2024-01-01/point?lat=-35&lon=145")
    assert response.status_code == 404


def test_point_missing_date():
    with patch("app.routers.shared.load_slice", side_effect=FileNotFoundError("No data")):
        response = client.get("/data_tiles/sea_level_anomaly/9999-01-01/point?lat=-35&lon=145")
    assert response.status_code == 404


def test_point_ok():
    with patch("app.routers.shared.load_slice", return_value=_make_ds()):
        response = client.get("/data_tiles/sea_level_anomaly/2024-01-01/point?lat=-35&lon=145")
    assert response.status_code == 200
    body = response.json()
    assert "lat" in body and "lon" in body and "variables" in body
    assert "GSLA" in body["variables"]


# --- /{product}/point (time series) ---


def test_point_series_unknown_product():
    response = client.get("/data_tiles/nonexistent/point?lat=-35&lon=145&from=2024-01-01")
    assert response.status_code == 404


def test_point_series_ok():
    all_dates = ["2023-12-01", "2024-01-01", "2024-01-15", "2024-02-01"]
    with (
        patch("app.routers.products.get_available_dates", return_value=all_dates),
        patch("app.routers.products.load_slice", return_value=_make_ds()),
    ):
        response = client.get(
            "/data_tiles/sea_level_anomaly/point?lat=-35&lon=145&from=2024-01-01&to=2024-01-31"
        )
    assert response.status_code == 200
    body = response.json()
    assert body["lat"] is not None and body["lon"] is not None
    assert [entry["date"] for entry in body["series"]] == ["2024-01-01", "2024-01-15"]
    assert "GSLA" in body["series"][0]["variables"]


def test_point_series_empty_range():
    with (
        patch("app.routers.products.get_available_dates", return_value=["2020-01-01"]),
        patch("app.routers.products.three_months_ago", return_value="2024-01-01"),
    ):
        response = client.get("/data_tiles/sea_level_anomaly/point?lat=-35&lon=145")
    assert response.status_code == 200
    body = response.json()
    assert body["series"] == []
    assert body["lat"] is None and body["lon"] is None


# --- /manifest (products availability) ---


def test_availability_ok():
    with (
        patch("app.routers.products.PRODUCTS", _FAKE_PRODUCTS),
        patch(
            "app.routers.products.get_available_dates", return_value=["2024-06-01", "2024-07-01"]
        ),
        patch("app.routers.products.three_months_ago", return_value="2024-01-01"),
    ):
        response = client.get("/data_tiles/manifest")
    assert response.status_code == 200
    body = response.json()
    assert body["products"] == {"product_a": {"available_dates": ["2024-06-01", "2024-07-01"]}}
    assert "cache_version" in body


def test_availability_date_filters():
    all_dates = ["2024-01-01", "2024-06-01", "2024-09-01", "2024-12-01"]
    with (
        patch("app.routers.products.PRODUCTS", _FAKE_PRODUCTS),
        patch("app.routers.products.get_available_dates", return_value=all_dates),
    ):
        response = client.get("/data_tiles/manifest?from=2024-06-01&to=2024-09-01")
    assert response.status_code == 200
    assert response.json()["products"]["product_a"]["available_dates"] == [
        "2024-06-01",
        "2024-09-01",
    ]


def test_availability_no_dates_in_range():
    with (
        patch("app.routers.products.PRODUCTS", _FAKE_PRODUCTS),
        patch("app.routers.products.get_available_dates", return_value=["2020-01-01"]),
        patch("app.routers.products.three_months_ago", return_value="2024-01-01"),
    ):
        response = client.get("/data_tiles/manifest")
    assert response.status_code == 200
    assert response.json()["products"]["product_a"]["available_dates"] == []
