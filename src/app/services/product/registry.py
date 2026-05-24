"""In-memory ``Product`` registry + ``products.json`` persistence.

Single front door for everything product-related at runtime:

  * The ``PRODUCTS`` dict is the canonical registered-product state. Internal
    consumers (test fixtures, the prewarm race-guard) still touch it directly
    where the dict's identity matters; production callers should go through
    the facades (``get_product``, ``iter_products``, ``iter_product_items``).
  * ``load_products`` / ``register_product`` / ``remove_product`` keep the
    on-disk ``products.json`` and the in-memory dict in sync; mutations are
    serialised under a module-level lock.
  * ``list_products`` returns the raw JSON entries (used by ``GET /products``);
    this is intentionally different from ``iter_products()`` which returns live
    ``Product`` instances.
"""

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from app.config.constants import TILE
from app.config.paths import PRODUCTS_CONFIG_PATH
from app.services.product.product import Product

logger = logging.getLogger(__name__)

_config_path = Path(PRODUCTS_CONFIG_PATH)
_lock = threading.Lock()

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

    Returns a list (not a view) so concurrent admin reloads can't raise
    ``RuntimeError: dictionary changed size during iteration`` in the caller's loop.
    """
    return list(PRODUCTS.values())


def iter_product_items() -> list[tuple[str, Product]]:
    """Snapshot of every (product_id, Product) pair. Snapshot rationale: see iter_products."""
    return list(PRODUCTS.items())


def load_products() -> None:
    """Read products.json from disk into PRODUCTS. Called on startup and after admin mutations.

    Updates PRODUCTS in place without ever exposing an empty state to concurrent readers:
    additions/updates are applied first, then removals. A reader that races a reload sees
    either the previous set, the new set, or a transient with stale entries still
    present — never an empty dict. This avoids 404s on /manifest during admin reloads.
    """
    if not _config_path.exists():
        logger.info("No products.json found — starting with empty product list")
        return
    entries: list[dict] = json.loads(_config_path.read_text())
    new = {entry["id"]: _from_dict(entry) for entry in entries}
    for product_id, product in new.items():
        PRODUCTS[product_id] = product
    for stale_id in [k for k in PRODUCTS if k not in new]:
        del PRODUCTS[stale_id]
    logger.info(
        "Loaded products from disk",
        extra={"count": len(PRODUCTS), "path": str(_config_path)},
    )


def register_product(entry: dict) -> Product:
    """Write new product to disk then reload. Raises ValueError if ID already exists."""
    with _lock:
        if entry["id"] in PRODUCTS:
            raise ValueError(f"Product '{entry['id']}' already exists")
        entries = _read_file()
        entries.append(entry)
        _write_file(entries)
        load_products()
        return PRODUCTS[entry["id"]]


def remove_product(product_id: str) -> None:
    """Remove product from disk then reload. Raises KeyError if not found."""
    with _lock:
        entries = _read_file()
        remaining = [e for e in entries if e["id"] != product_id]
        if len(remaining) == len(entries):
            raise KeyError(product_id)
        _write_file(remaining)
        load_products()


def list_products() -> list[dict]:
    """Return the raw JSON entries from products.json. Used by ``GET /products``.

    Distinct from ``iter_products()`` which returns live ``Product`` instances.
    This returns whatever the config file says, including fields that may not
    be on the Product dataclass.
    """
    with _lock:
        return _read_file()


def _read_file() -> list[dict]:
    if not _config_path.exists():
        return []
    return json.loads(_config_path.read_text())


def _write_file(entries: list[dict]) -> None:
    # Atomic write: tempfile in the same dir, then os.replace.
    data = json.dumps(entries, indent=2)
    directory = _config_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=_config_path.name + ".", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp_path, _config_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _from_dict(entry: dict) -> Product:
    chunk_px = entry.get("chunk_px", list(TILE.chunk_px))
    return Product(
        id=entry["id"],
        source_path=entry["source_path"],
        variable=entry["variable"],
        chunk_px=tuple(chunk_px),  # type: ignore[arg-type]
        padding=entry.get("padding", TILE.padding),
    )
