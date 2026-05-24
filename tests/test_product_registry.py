"""product/registry: JSON round-trip + the no-empty-state guarantee from the docstring.

load_products documents that concurrent readers never see an empty PRODUCTS
dict during reload — additions happen before removals. We pin that ordering
along with the basic CRUD persistence.
"""

import json

import pytest

import app.services.product.registry as registry
from app.services.product.product import Product
from app.services.product.registry import PRODUCTS


@pytest.fixture
def isolated_products(tmp_path, monkeypatch):
    """Redirect registry at a tmp file and snapshot PRODUCTS."""
    cfg = tmp_path / "products.json"
    monkeypatch.setattr(registry, "_config_path", cfg)
    saved = dict(PRODUCTS)
    PRODUCTS.clear()
    yield cfg
    PRODUCTS.clear()
    PRODUCTS.update(saved)


def _entry(product_id="p1", source="s3://bucket/x.zarr", variable="V"):
    return {"id": product_id, "source_path": source, "variable": variable}


def test_load_products_no_file_is_noop(isolated_products):
    registry.load_products()
    assert PRODUCTS == {}


def test_register_then_persist_and_reload(isolated_products):
    registry.register_product(_entry("p1"))
    assert "p1" in PRODUCTS
    on_disk = json.loads(isolated_products.read_text())
    assert on_disk[0]["id"] == "p1"

    # Reload from disk (no in-memory bypass) — should match.
    PRODUCTS.clear()
    registry.load_products()
    assert PRODUCTS["p1"].source_path == "s3://bucket/x.zarr"


def test_register_duplicate_raises(isolated_products):
    registry.register_product(_entry("p1"))
    with pytest.raises(ValueError, match="already exists"):
        registry.register_product(_entry("p1"))


def test_register_with_multi_variable(isolated_products):
    """variable can be a list — exercised by the ocean_current fixture."""
    registry.register_product(_entry("multi", variable=["U", "V"]))
    p = PRODUCTS["multi"]
    assert p.variables == ["U", "V"]


def test_register_with_chunk_px_and_padding(isolated_products):
    registry.register_product(
        {
            "id": "tuned",
            "source_path": "s3://bucket/x.zarr",
            "variable": "V",
            "chunk_px": [128, 96],
            "padding": 4,
        }
    )
    p = PRODUCTS["tuned"]
    assert p.chunk_px == (128, 96)
    assert p.padding == 4


def test_remove_product_persists_and_reflects_in_memory(isolated_products):
    registry.register_product(_entry("p1"))
    registry.register_product(_entry("p2", source="s3://bucket/y.zarr"))
    assert set(PRODUCTS.keys()) == {"p1", "p2"}

    registry.remove_product("p1")

    assert set(PRODUCTS.keys()) == {"p2"}
    on_disk = json.loads(isolated_products.read_text())
    assert [e["id"] for e in on_disk] == ["p2"]


def test_remove_unknown_raises_keyerror(isolated_products):
    registry.register_product(_entry("p1"))
    with pytest.raises(KeyError):
        registry.remove_product("nope")
    # p1 should still be there.
    assert "p1" in PRODUCTS


def test_list_products_reflects_file_contents(isolated_products):
    registry.register_product(_entry("a"))
    registry.register_product(_entry("b", source="s3://bucket/y.zarr"))
    listed = registry.list_products()
    assert [e["id"] for e in listed] == ["a", "b"]


def test_load_products_never_exposes_empty_state(isolated_products, monkeypatch):
    """Documented invariant: additions first, then removals — readers never see {}.

    Strategy: replace PRODUCTS with a subclass that snapshots keys on every
    __delitem__, then verify the new entry is already present at every
    removal point.
    """
    observed_snapshots: list[set[str]] = []

    class SpyDict(dict):
        def __delitem__(self, key):
            observed_snapshots.append(set(self.keys()))
            super().__delitem__(key)

    spy = SpyDict()
    spy["a"] = registry._from_dict(_entry("a"))
    spy["b"] = registry._from_dict(_entry("b", source="s3://bucket/y.zarr"))

    # Replace the module-level PRODUCTS dict so load_products mutates the spy.
    monkeypatch.setattr(registry, "PRODUCTS", spy)

    # On-disk replaces both with 'c'.
    isolated_products.write_text(json.dumps([_entry("c", source="s3://bucket/z.zarr")]))
    registry.load_products()

    # By the time a stale key is removed, the new entry must already be present.
    assert observed_snapshots, "expected at least one removal during reload"
    for snapshot in observed_snapshots:
        assert "c" in snapshot, (
            "PRODUCTS exposed a state without the new entry — "
            "remove-before-add ordering breaks the no-empty-state invariant"
        )

    assert set(spy.keys()) == {"c"}


def test_load_malformed_json_raises(isolated_products):
    isolated_products.write_text("not json at all")
    with pytest.raises(json.JSONDecodeError):
        registry.load_products()


def test_from_dict_returns_frozen_product(isolated_products):
    from dataclasses import FrozenInstanceError

    registry.register_product(_entry("frozen"))
    p = PRODUCTS["frozen"]
    assert isinstance(p, Product)
    # Frozen dataclass: assignment must raise.
    with pytest.raises(FrozenInstanceError):
        p.id = "changed"  # type: ignore[misc]


def test_register_returns_the_product_object(isolated_products):
    p = registry.register_product(_entry("returned"))
    assert isinstance(p, Product)
    assert p.id == "returned"
    assert p is PRODUCTS["returned"]


def test_write_uses_indented_json(isolated_products):
    registry.register_product(_entry("readable"))
    raw = isolated_products.read_text()
    assert "\n" in raw, "indent=2 missing"
