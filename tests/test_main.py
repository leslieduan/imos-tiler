from unittest.mock import patch

from starlette.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=True)

# A PNG large enough to clear GZipMiddleware's minimum_size, so the no-gzip assertion
# below exercises the content-type exclusion rather than passing on the size threshold.
_BIG_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000


def test_root():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_schema():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "paths" in response.json()


def test_gzip_compresses_json():
    # /openapi.json is well over minimum_size, so a gzip-capable client must get it compressed.
    response = client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"


def test_gzip_skips_image_tiles():
    # PNG tiles are already compressed; gzipping them wastes CPU. The image/* deny-list
    # entry must keep them uncompressed even though the body exceeds minimum_size and the
    # client advertises gzip. This guards the Starlette-global monkeypatch in main.py.
    with (
        patch("app.routers.shared.load_slice"),
        patch("app.routers.visual_tiles.render_tile", return_value=_BIG_PNG),
    ):
        response = client.get(
            "/visual_tiles/sea_level_anomaly/2024-01-01/5/0/0.png",
            headers={"Accept-Encoding": "gzip"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers.get("content-encoding") != "gzip"
