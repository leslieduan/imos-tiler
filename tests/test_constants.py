import pytest

from constants import Product


def test_compute_lod_grids_returns_nonempty():
    grids = Product._compute_lod_grids(3600, 1800, (240, 192))
    assert len(grids) > 0


def test_compute_lod_grids_keys_are_sequential():
    grids = Product._compute_lod_grids(3600, 1800, (240, 192))
    assert list(grids.keys()) == list(range(1, len(grids) + 1))


def test_compute_lod_grids_respects_max_lods():
    grids = Product._compute_lod_grids(3600, 1800, (240, 192), max_lods=2)
    assert len(grids) <= 2


def test_compute_lod_grids_respects_min_coarsest():
    # min_coarsest of (10, 10) should filter out levels smaller than that
    grids = Product._compute_lod_grids(3600, 1800, (240, 192), min_coarsest=(10, 10))
    for cols, rows in grids.values():
        assert cols >= 10 and rows >= 10


def test_compute_lod_grids_small_data():
    # data smaller than chunk_px → finest grid is (1,1) which is below MIN_COARSEST_GRID=(2,2)
    grids = Product._compute_lod_grids(100, 100, (240, 192))
    assert grids == {}


def test_compute_lod_grids_small_data_relaxed_min():
    # with min_coarsest=(1,1) the single level should survive
    grids = Product._compute_lod_grids(100, 100, (240, 192), min_coarsest=(1, 1))
    assert grids == {1: (1, 1)}


@pytest.mark.parametrize("product_id", ["zarr_sea_level_anomaly", "zarr_ocean_current"])
def test_zarr_products_have_no_lod_grids_by_default(product_id: str):
    from constants import ZARR_PRODUCTS

    product = ZARR_PRODUCTS[product_id]
    assert product.lod_grids == {}


def test_apply_computed_lod_grids_is_noop_when_already_set():
    from constants import SST_ANOM_MOSAIC

    original = dict(SST_ANOM_MOSAIC.lod_grids)
    SST_ANOM_MOSAIC.apply_computed_lod_grids(9999, 9999)
    assert SST_ANOM_MOSAIC.lod_grids == original
