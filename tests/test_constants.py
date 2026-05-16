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
    # data smaller than chunk_px → all levels filtered by LOD.min_coarsest, falls back to native finest grid
    grids = Product._compute_lod_grids(100, 100, (240, 192))
    assert grids == {1: (1, 1)}


def test_compute_lod_grids_small_data_one_axis():
    # one axis too small → fallback grid reflects actual dimensions
    grids = Product._compute_lod_grids(480, 74, (240, 192))
    assert grids == {1: (2, 1)}


def test_products_have_no_lod_grids_by_default():
    p = Product(id="test", source_path="s3://test", variable="VAR")
    assert p.lod_grids == {}


def test_apply_computed_lod_grids_is_noop_when_already_set():
    p = Product(id="test", source_path="s3://test", variable="x", lod_grids={1: (2, 2)})
    p.apply_computed_lod_grids(9999, 9999)
    assert p.lod_grids == {1: (2, 2)}
