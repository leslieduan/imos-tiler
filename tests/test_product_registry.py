"""product/registry: JSON load + the no-empty-state guarantee from the docstring.

load_products documents that concurrent readers never see an empty PRODUCTS
dict during reload — additions happen before removals. We pin that ordering
along with basic load behavior. Products are static config now (no admin
API), so these tests write products.json directly rather than going through
a registration call.
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


def _entry(product_id="p1", source="s3://bucket/x.zarr", variable="V", **kwargs):
    return {"id": product_id, "source_path": source, "variable": variable, **kwargs}


def _write(cfg, entries):
    cfg.write_text(json.dumps(entries))


def test_load_products_no_file_is_noop(isolated_products):
    registry.load_products()
    assert PRODUCTS == {}


def test_load_populates_products(isolated_products):
    _write(isolated_products, [_entry("p1")])
    registry.load_products()
    assert PRODUCTS["p1"].source_path == "s3://bucket/x.zarr"


def test_load_with_multi_variable(isolated_products):
    """variable can be a list — exercised by the ocean_current fixture."""
    _write(isolated_products, [_entry("multi", variable=["U", "V"])])
    registry.load_products()
    assert PRODUCTS["multi"].variables == ["U", "V"]


def test_load_with_chunk_px_and_padding(isolated_products):
    _write(
        isolated_products,
        [
            {
                "id": "tuned",
                "source_path": "s3://bucket/x.zarr",
                "variable": "V",
                "chunk_px": [128, 96],
                "padding": 4,
            }
        ],
    )
    registry.load_products()
    p = PRODUCTS["tuned"]
    assert p.chunk_px == (128, 96)
    assert p.padding == 4


def test_load_with_coastal_fill(isolated_products):
    _write(
        isolated_products,
        [
            {
                "id": "sparse",
                "source_path": "s3://bucket/x.zarr",
                "variable": "V",
                "coastal_fill": {"max_dist_px": 4},
            }
        ],
    )
    registry.load_products()
    p = PRODUCTS["sparse"]
    assert p.coastal_fill is not None
    assert p.coastal_fill.max_dist_px == 4


def test_coastal_fill_absent_defaults_to_none(isolated_products):
    _write(isolated_products, [_entry("plain")])
    registry.load_products()
    assert PRODUCTS["plain"].coastal_fill is None


def test_ocean_masked_absent_defaults_to_false(isolated_products):
    _write(isolated_products, [_entry("plain")])
    registry.load_products()
    assert PRODUCTS["plain"].ocean_masked is False


def test_ocean_masked_defaults_true_for_listed_product(isolated_products):
    # The currents product is masked by default even without the config flag.
    pid = "model_sea_level_anomaly_gridded_realtime_vcur_ucur"
    _write(isolated_products, [_entry(pid, variable=["UCUR", "VCUR"])])
    registry.load_products()
    assert PRODUCTS[pid].ocean_masked is True


def test_ocean_masked_explicit_false_overrides_default(isolated_products):
    pid = "model_sea_level_anomaly_gridded_realtime_vcur_ucur"
    _write(isolated_products, [_entry(pid, variable=["UCUR", "VCUR"], ocean_masked=False)])
    registry.load_products()
    assert PRODUCTS[pid].ocean_masked is False


def test_list_products_reflects_file_contents(isolated_products):
    _write(isolated_products, [_entry("a"), _entry("b", source="s3://bucket/y.zarr")])
    listed = registry.list_products()
    assert [e["id"] for e in listed] == ["a", "b"]


def test_list_products_no_file_returns_empty(isolated_products):
    assert registry.list_products() == []


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


def test_from_dict_returns_frozen_product():
    from dataclasses import FrozenInstanceError

    p = registry._from_dict(_entry("frozen"))
    assert isinstance(p, Product)
    # Frozen dataclass: assignment must raise.
    with pytest.raises(FrozenInstanceError):
        p.id = "changed"  # type: ignore[misc]
