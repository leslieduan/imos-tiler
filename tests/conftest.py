import pytest

from constants import PRODUCTS, Product


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
