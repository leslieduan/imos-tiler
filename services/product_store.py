import json
import logging
from pathlib import Path

from constants import CHUNK_PX, PADDING, PRODUCTS, PRODUCTS_CONFIG_PATH, Product

logger = logging.getLogger("services")

_config_path = Path(PRODUCTS_CONFIG_PATH)


def load_products() -> None:
    """Read products.json from disk into PRODUCTS. Called once on startup."""
    if not _config_path.exists():
        logger.info("No products.json found — starting with empty product list")
        return
    entries: list[dict] = json.loads(_config_path.read_text())
    PRODUCTS.clear()
    for entry in entries:
        product = _from_dict(entry)
        PRODUCTS[product.id] = product
    logger.info("Loaded %d product(s) from %s", len(PRODUCTS), _config_path)


def register_product(entry: dict) -> Product:
    """Write new product to disk then reload. Raises ValueError if ID already exists."""
    if entry["id"] in PRODUCTS:
        raise ValueError(f"Product '{entry['id']}' already exists")
    entries = _read_file()
    entries.append(entry)
    _write_file(entries)
    load_products()
    return PRODUCTS[entry["id"]]


def remove_product(product_id: str) -> None:
    """Remove product from disk then reload. Raises KeyError if not found."""
    entries = _read_file()
    remaining = [e for e in entries if e["id"] != product_id]
    if len(remaining) == len(entries):
        raise KeyError(product_id)
    _write_file(remaining)
    load_products()


def list_products() -> list[dict]:
    return _read_file()


def _read_file() -> list[dict]:
    if not _config_path.exists():
        return []
    return json.loads(_config_path.read_text())


def _write_file(entries: list[dict]) -> None:
    _config_path.write_text(json.dumps(entries, indent=2))


def _from_dict(entry: dict) -> Product:
    chunk_px = entry.get("chunk_px", list(CHUNK_PX))
    return Product(
        id=entry["id"],
        source_path=entry["source_path"],
        variable=entry.get("variable", ""),
        chunk_px=tuple(chunk_px),  # type: ignore[arg-type]
        padding=entry.get("padding", PADDING),
    )
