"""Lock down cross-system invariants documented in CLAUDE.md.

These tests guard contracts that span the server and the frontend WebGL shader,
or that depend on environment configuration that has historically caused silent
failures (timezone handling, the two tile coordinate systems). If any of these
fail, the change needs a coordinated review — not a test update.
"""

from unittest.mock import patch

import numpy as np
import xarray as xr
from starlette.testclient import TestClient

from app.config.constants import CACHE_VERSION, LOD, TILE
from app.main import app
from app.services.product.product import Product

client = TestClient(app, raise_server_exceptions=True)


# --- LOD / tile geometry contract -----------------------------------------
#
# LODConfig and TileConfig are baked into the frontend WebGL shader's texture
# atlas layout. Changing any of these values without a coordinated frontend
# redeploy silently corrupts rendering — no error, just wrong pixels. CACHE_VERSION
# is the manifest-level cache-buster the frontend uses to invalidate tile URLs
# when render output changes; bumping it without intent breaks all caches.


def test_lod_config_matches_frontend_shader_contract():
    assert LOD.max_lods == 4
    assert LOD.min_coarsest == (2, 2)
    assert LOD.zoom_thresholds == {2: 4, 3: 5, 4: 6}


def test_tile_geometry_matches_frontend_shader_contract():
    assert TILE.chunk_px == (240, 192)
    assert TILE.padding == 1


def test_cache_version_is_set():
    # Bumping is intentional; an unintended change to CACHE_VERSION invalidates
    # every CDN/browser-cached tile across all clients on next deploy.
    assert CACHE_VERSION == "cv2"


# --- Date / timezone round-trip ------------------------------------------
#
# API dates are TILE_TIMEZONE local (default Australia/Sydney), not UTC. The
# Zarr store is UTC. A date pulled from /manifest must be acceptable when sent
# back to /data_tiles — clients are required to round-trip the strings unchanged.


_LOD_GRIDS = {1: (1, 1)}
_FAKE_PRODUCTS = {
    "sea_level_anomaly": Product(
        id="sea_level_anomaly", source_path="s3://bucket/a.zarr", variable="GSLA"
    ),
}


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


def test_date_from_manifest_is_accepted_by_tile_endpoint():
    available = ["2024-06-01", "2024-07-01"]
    with (
        patch(
            "app.routers.products.iter_product_items",
            return_value=list(_FAKE_PRODUCTS.items()),
        ),
        patch("app.routers.products.get_available_dates", return_value=available),
    ):
        manifest = client.get("/data_tiles/manifest")
    assert manifest.status_code == 200
    dates = manifest.json()["products"]["sea_level_anomaly"]["available_dates"]
    assert dates, "manifest returned no dates — fixture broken"
    date_str = dates[0]

    # Send that date back unchanged. If the server's local-vs-UTC handling
    # silently drifts, this round-trip will start returning 404.
    with (
        patch("app.routers.data_tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.data_tiles.render_tile", return_value=b"\x89PNG\r\n\x1a\n"),
    ):
        tile = client.get(f"/data_tiles/sea_level_anomaly/{date_str}/1/0/0.png")
    assert tile.status_code == 200, f"date {date_str!r} from /manifest was rejected by /data_tiles"


# --- Two tile coordinate systems must not alias -------------------------
#
# /data_tiles uses Plate Carrée chunk indices; /visual_tiles uses Web Mercator
# XYZ. Same z/x/y values must NOT produce the same bytes — if a refactor ever
# accidentally fused the two render paths, this test catches it.


def test_data_and_visual_tiles_route_to_different_renderers():
    # Mock each router's renderer with distinct bytes. If the two routes ever
    # collapsed into one (or both started pointing at the same renderer module),
    # only one of these patches would take effect and the responses would match.
    z, x, y = 1, 0, 0
    data_bytes = b"\x89PNG\r\n\x1a\nDATA"
    visual_bytes = b"\x89PNG\r\n\x1a\nVISUAL"

    with (
        patch("app.routers.data_tiles.get_lod_grids", return_value=_LOD_GRIDS),
        patch("app.routers.shared.load_slice", return_value=_make_ds()),
        patch("app.routers.data_tiles.render_tile", return_value=data_bytes),
        patch("app.routers.visual_tiles.render_tile", return_value=visual_bytes),
    ):
        data_resp = client.get(f"/data_tiles/sea_level_anomaly/2024-01-01/{z}/{x}/{y}.png")
        visual_resp = client.get(f"/visual_tiles/sea_level_anomaly/2024-01-01/{z}/{x}/{y}.png")

    assert data_resp.status_code == 200, data_resp.text
    assert visual_resp.status_code == 200, visual_resp.text
    assert data_resp.content == data_bytes
    assert visual_resp.content == visual_bytes
