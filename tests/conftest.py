import os

# Pin the cache backend before any `app` import runs `load_dotenv()`, so the
# suite is hermetic regardless of CACHE_BACKEND set in a developer's local
# .env for manual testing of the redis/none backends.
os.environ["CACHE_BACKEND"] = "memory"

import pytest

from app.services.product.product import Product
from app.services.product.registry import PRODUCTS


@pytest.fixture(autouse=True)
def seed_products():
    """Populate PRODUCTS with test fixtures before each test and clean up after."""
    test_products = [
        Product(id="sea_level_anomaly", source_path="s3://test/sla.zarr", variable="GSLA"),
        Product(id="ocean_current", source_path="s3://test/sla.zarr", variable=["UCUR", "VCUR"]),
    ]
    for p in test_products:
        PRODUCTS[p.id] = p
    yield
    for p in test_products:
        PRODUCTS.pop(p.id, None)
