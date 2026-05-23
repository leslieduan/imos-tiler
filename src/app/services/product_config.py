import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from app.constants import CHUNK_PX, PADDING, PRODUCTS, PRODUCTS_CONFIG_PATH
from app.domain.product import Product

logger = logging.getLogger(__name__)

_config_path = Path(PRODUCTS_CONFIG_PATH)
_lock = threading.Lock()


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
    chunk_px = entry.get("chunk_px", list(CHUNK_PX))
    return Product(
        id=entry["id"],
        source_path=entry["source_path"],
        variable=entry["variable"],
        chunk_px=tuple(chunk_px),  # type: ignore[arg-type]
        padding=entry.get("padding", PADDING),
    )
