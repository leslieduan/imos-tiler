"""In-memory ``Product`` registry, loaded from ``products.json`` at startup.

Single front door for everything product-related at runtime:

  * The ``PRODUCTS`` dict is the canonical registered-product state. Internal
    consumers (test fixtures, the prewarm race-guard) still touch it directly
    where the dict's identity matters; production callers should go through
    the facades (``get_product``, ``iter_products``, ``iter_product_items``).
  * ``load_products`` reads the on-disk ``products.json`` into the in-memory
    dict. Products are static config (``config/products.json``) — add or
    remove one by editing the file and redeploying.
  * ``list_products`` returns the raw JSON entries (used by ``GET /products``);
    this is intentionally different from ``iter_products()`` which returns live
    ``Product`` instances.
"""

import json
from pathlib import Path

from app.config.constants import TILE
from app.config.paths import PRODUCTS_CONFIG_PATH
from app.services.product.product import CoastalFill, Product

_config_path = Path(PRODUCTS_CONFIG_PATH)

# Products that are ocean-masked unless products.json says otherwise. The committed
# ocean mask is built from this store's grid, so masking is the safe default for it
# and shouldn't depend on the config flag being remembered. An explicit
# "ocean_masked": false in products.json still wins.
_OCEAN_MASKED_BY_DEFAULT = frozenset(
    {
        "model_sea_level_anomaly_gridded_realtime_vcur_ucur",
    }
)

# Canonical registered-product state. Exposed (rather than wrapped behind a
# class) because the dict identity is load-bearing for test fixtures and for
# the prewarm race-guard ``PRODUCTS.get(p.id) is not p`` check — both rely on
# the same Python object being mutated in place.
PRODUCTS: dict[str, Product] = {}


def get_product(product_id: str) -> Product | None:
    """Return the registered Product for ``product_id``, or None if not registered."""
    return PRODUCTS.get(product_id)


def iter_products() -> list[Product]:
    """Snapshot of every registered Product.

    Returns a list (not a view) so a concurrent reload can't raise
    ``RuntimeError: dictionary changed size during iteration`` in the caller's loop.
    """
    return list(PRODUCTS.values())


def iter_product_items() -> list[tuple[str, Product]]:
    """Snapshot of every (product_id, Product) pair. Snapshot rationale: see iter_products."""
    return list(PRODUCTS.items())


def load_products() -> None:
    """Read products.json from disk into PRODUCTS. Called once on startup.

    Updates PRODUCTS in place without ever exposing an empty state to concurrent readers:
    additions/updates are applied first, then removals. A reader that races a reload sees
    either the previous set, the new set, or a transient with stale entries still
    present — never an empty dict.
    """
    if not _config_path.exists():
        print("No products.json found — starting with empty product list")
        return
    entries: list[dict] = json.loads(_config_path.read_text())
    new = {entry["id"]: _from_dict(entry) for entry in entries}
    for product_id, product in new.items():
        PRODUCTS[product_id] = product
    for stale_id in [k for k in PRODUCTS if k not in new]:
        del PRODUCTS[stale_id]
    print(f"Loaded {len(PRODUCTS)} products from {_config_path}")


def list_products() -> list[dict]:
    """Return the raw JSON entries from products.json. Used by ``GET /products``.

    Distinct from ``iter_products()`` which returns live ``Product`` instances.
    This returns whatever the config file says, including fields that may not
    be on the Product dataclass.
    """
    if not _config_path.exists():
        return []
    return json.loads(_config_path.read_text())


def _from_dict(entry: dict) -> Product:
    chunk_px = entry.get("chunk_px", list(TILE.chunk_px))
    coastal_fill = entry.get("coastal_fill")
    return Product(
        id=entry["id"],
        source_path=entry["source_path"],
        variable=entry["variable"],
        chunk_px=tuple(chunk_px),  # type: ignore[arg-type]
        padding=entry.get("padding", TILE.padding),
        coastal_fill=CoastalFill(**coastal_fill) if coastal_fill else None,
        ocean_masked=entry.get("ocean_masked", entry["id"] in _OCEAN_MASKED_BY_DEFAULT),
    )
