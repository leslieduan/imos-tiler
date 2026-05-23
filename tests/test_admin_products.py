"""Admin CRUD for products: POST 201, DELETE 204, error mapping, auth, prewarm spawn."""

import asyncio
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.routers.admin import products as admin_products
from app.services.product.product import Product
from app.services.product.registry import PRODUCTS

client = TestClient(app, raise_server_exceptions=True)

_ADMIN_KEY = "test-secret"
_HEADERS = {"X-Admin-Key": _ADMIN_KEY}


@pytest.fixture(autouse=True)
def admin_key_env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", _ADMIN_KEY)


@pytest.fixture
def quiet_prewarm(monkeypatch):
    """Stop the admin handler from scheduling real background tasks.

    Not autouse: the _spawn_prewarm test needs the real implementation.
    """
    monkeypatch.setattr(admin_products, "_spawn_prewarm", lambda _product: None)


@pytest.fixture(autouse=True)
def _default_quiet(request, monkeypatch):
    """Apply quiet_prewarm by default; tests that exercise _spawn_prewarm opt out
    by marking with @pytest.mark.real_prewarm."""
    if "real_prewarm" in request.keywords:
        return
    monkeypatch.setattr(admin_products, "_spawn_prewarm", lambda _product: None)


@pytest.fixture(autouse=True)
def _stub_validate_store(request, monkeypatch):
    """No-op the store test-open by default; tests that exercise validation opt
    out by marking with @pytest.mark.real_validate."""
    if "real_validate" in request.keywords:
        return
    monkeypatch.setattr(admin_products, "_validate_store", lambda _payload: None)


# ---------------------------------------------------------------------------
# POST /admin/products — success
# ---------------------------------------------------------------------------


def test_add_product_returns_201_and_id():
    payload = {"id": "new_prod", "source_path": "s3://b/x.zarr", "variable": "V"}
    fake = Product(id="new_prod", source_path="s3://b/x.zarr", variable="V")
    with patch("app.routers.admin.products.register_product", return_value=fake) as reg:
        r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 201
    assert r.json() == {"id": "new_prod", "source_path": "s3://b/x.zarr"}
    reg.assert_called_once()


def test_add_product_with_chunk_px_and_padding():
    payload = {
        "id": "tuned",
        "source_path": "s3://b/x.zarr",
        "variable": "V",
        "chunk_px": [128, 96],
        "padding": 4,
    }
    fake = Product(id="tuned", source_path="s3://b/x.zarr", variable="V")
    with patch("app.routers.admin.products.register_product", return_value=fake):
        r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 201


def test_add_product_multi_variable():
    payload = {"id": "multi", "source_path": "s3://b/x.zarr", "variable": ["U", "V"]}
    fake = Product(id="multi", source_path="s3://b/x.zarr", variable=["U", "V"])
    with patch("app.routers.admin.products.register_product", return_value=fake):
        r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# POST /admin/products — validation
# ---------------------------------------------------------------------------


def test_add_product_empty_id_rejected():
    payload = {"id": "", "source_path": "s3://b/x.zarr", "variable": "V"}
    r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 422


def test_add_product_whitespace_only_id_rejected():
    payload = {"id": "   ", "source_path": "s3://b/x.zarr", "variable": "V"}
    r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 422


def test_add_product_id_with_trailing_whitespace_rejected():
    payload = {"id": "abc ", "source_path": "s3://b/x.zarr", "variable": "V"}
    r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 422


def test_add_product_empty_source_path_rejected():
    payload = {"id": "p", "source_path": "   ", "variable": "V"}
    r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 422


@pytest.mark.parametrize(
    "chunk_px",
    [
        [128],  # wrong length
        [128, 96, 32],  # too many
        [0, 96],  # zero
        [-1, 96],  # negative
    ],
)
def test_add_product_bad_chunk_px_rejected(chunk_px):
    payload = {
        "id": "p",
        "source_path": "s3://b/x.zarr",
        "variable": "V",
        "chunk_px": chunk_px,
    }
    r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /admin/products — store validation (test-open before persist)
# ---------------------------------------------------------------------------


@pytest.mark.real_validate
def test_add_product_bad_url_returns_400():
    payload = {"id": "bogus", "source_path": "s3://no/such.zarr", "variable": "V"}
    with (
        patch(
            "app.routers.admin.products.get_store",
            side_effect=FileNotFoundError("no such bucket/key"),
        ),
        patch("app.routers.admin.products.register_product") as reg,
    ):
        r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 400
    assert "Cannot open store" in r.json()["detail"]
    reg.assert_not_called()  # bad URL must not get persisted


@pytest.mark.real_validate
def test_add_product_missing_variable_returns_400():
    """Store opens, but declared variable isn't in data_vars → 400, no persistence."""
    import xarray as xr

    fake_store = xr.Dataset({"OTHER": ("x", [1.0])})
    payload = {"id": "p", "source_path": "s3://b/x.zarr", "variable": "MISSING"}
    with (
        patch("app.routers.admin.products.get_store", return_value=fake_store),
        patch("app.routers.admin.products.register_product") as reg,
    ):
        r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 400
    assert "MISSING" in r.json()["detail"]
    reg.assert_not_called()


@pytest.mark.real_validate
def test_add_product_multi_variable_partial_missing_returns_400():
    import xarray as xr

    fake_store = xr.Dataset({"U": ("x", [1.0])})
    payload = {"id": "p", "source_path": "s3://b/x.zarr", "variable": ["U", "V"]}
    with (
        patch("app.routers.admin.products.get_store", return_value=fake_store),
        patch("app.routers.admin.products.register_product") as reg,
    ):
        r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 400
    assert "V" in r.json()["detail"]
    reg.assert_not_called()


# ---------------------------------------------------------------------------
# POST /admin/products — error mapping
# ---------------------------------------------------------------------------


def test_add_product_duplicate_returns_409():
    payload = {"id": "dup", "source_path": "s3://b/x.zarr", "variable": "V"}
    with patch(
        "app.routers.admin.products.register_product",
        side_effect=ValueError("Product 'dup' already exists"),
    ):
        r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_add_product_persistence_failure_returns_500():
    payload = {"id": "p", "source_path": "s3://b/x.zarr", "variable": "V"}
    with patch(
        "app.routers.admin.products.register_product",
        side_effect=OSError("disk full"),
    ):
        r = client.post("/admin/products", json=payload, headers=_HEADERS)
    assert r.status_code == 500
    assert "Failed to persist product" in r.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /admin/products/{id}
# ---------------------------------------------------------------------------


def test_delete_product_returns_204_and_evicts_cache(monkeypatch):
    target = Product(id="to_delete", source_path="s3://b/x.zarr", variable="V")
    monkeypatch.setitem(PRODUCTS, "to_delete", target)

    with (
        patch("app.routers.admin.products.remove_product") as rem,
        patch("app.routers.admin.products.evict_product_cache") as evict,
    ):
        r = client.delete("/admin/products/to_delete", headers=_HEADERS)

    assert r.status_code == 204
    rem.assert_called_once_with("to_delete")
    evict.assert_called_once_with(target)


def test_delete_product_unknown_returns_404():
    with patch("app.routers.admin.products.remove_product", side_effect=KeyError("not found")):
        r = client.delete("/admin/products/never_existed", headers=_HEADERS)
    assert r.status_code == 404


def test_delete_product_persistence_failure_returns_500(monkeypatch):
    monkeypatch.setitem(PRODUCTS, "p", Product(id="p", source_path="s3://b/x.zarr", variable="V"))
    with patch("app.routers.admin.products.remove_product", side_effect=OSError("disk")):
        r = client.delete("/admin/products/p", headers=_HEADERS)
    assert r.status_code == 500


def test_delete_product_value_error_returns_403(monkeypatch):
    """ValueError from remove_product (e.g. permission/policy) maps to 403."""
    monkeypatch.setitem(PRODUCTS, "p", Product(id="p", source_path="s3://b/x.zarr", variable="V"))
    with patch(
        "app.routers.admin.products.remove_product",
        side_effect=ValueError("policy denied"),
    ):
        r = client.delete("/admin/products/p", headers=_HEADERS)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_post_without_admin_key_returns_401():
    payload = {"id": "p", "source_path": "s3://b/x.zarr", "variable": "V"}
    r = client.post("/admin/products", json=payload)
    assert r.status_code == 401


def test_delete_without_admin_key_returns_401():
    r = client.delete("/admin/products/p")
    assert r.status_code == 401


def test_post_wrong_admin_key_returns_403():
    payload = {"id": "p", "source_path": "s3://b/x.zarr", "variable": "V"}
    r = client.post("/admin/products", json=payload, headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# _spawn_prewarm: anchoring + cleanup
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.real_prewarm
async def test_spawn_prewarm_anchors_task_and_cleans_up_on_completion():
    """The task should be in _background_tasks while running and removed when done."""
    completed = asyncio.Event()

    def fake_prewarm(_products):
        completed.set()

    with patch("app.routers.admin.products.prewarm_disk_slices", side_effect=fake_prewarm):
        admin_products._background_tasks.clear()
        p = Product(id="bg", source_path="s3://b/x.zarr", variable="V")
        admin_products._spawn_prewarm(p)

        # Immediately after scheduling, the task is anchored.
        assert len(admin_products._background_tasks) == 1

        # Let the loop run the to_thread → done callback.
        for _ in range(20):
            if not admin_products._background_tasks:
                break
            await asyncio.sleep(0.05)

        assert completed.is_set()
        assert len(admin_products._background_tasks) == 0


@pytest.fixture
def anyio_backend():
    return "asyncio"
