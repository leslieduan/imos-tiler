"""Hex/RGBA parsing and LUT-building edge cases for utils.colors."""

import pytest

from app.utils.colors import build_categorical_lut, hex_to_rgba, interpolate_colormap, parse_color


def test_hex_to_rgba_6_char():
    assert hex_to_rgba("#ff0080") == [255, 0, 128, 255]


def test_hex_to_rgba_8_char_includes_alpha():
    assert hex_to_rgba("#ff008040") == [255, 0, 128, 64]


def test_hex_to_rgba_3_char_expands():
    """#abc → #aabbcc."""
    assert hex_to_rgba("#abc") == [170, 187, 204, 255]


def test_hex_to_rgba_no_hash_prefix():
    assert hex_to_rgba("ff0080") == [255, 0, 128, 255]


@pytest.mark.parametrize("bad", ["#ff", "#fffff", "#fffffffff", "nope", ""])
def test_hex_to_rgba_invalid_lengths_raise(bad):
    with pytest.raises(ValueError, match="invalid hex"):
        hex_to_rgba(bad)


def test_parse_color_accepts_hex_string():
    assert parse_color("#000000", "test") == [0, 0, 0, 255]


def test_parse_color_accepts_rgba_list():
    assert parse_color([10, 20, 30, 40], "test") == [10, 20, 30, 40]


def test_parse_color_accepts_rgba_tuple():
    assert parse_color((10, 20, 30, 40), "test") == [10, 20, 30, 40]


@pytest.mark.parametrize(
    "bad",
    [
        [255, 0, 0],  # too short
        [255, 0, 0, 0, 0],  # too long
        [256, 0, 0, 0],  # out of range
        [-1, 0, 0, 0],  # out of range
        ["255", 0, 0, 0],  # wrong type
    ],
)
def test_parse_color_invalid_list_raises(bad):
    with pytest.raises(ValueError, match="\\[r, g, b, a\\]"):
        parse_color(bad, "test_label")


def test_parse_color_invalid_type_raises():
    with pytest.raises(ValueError, match="hex string or"):
        parse_color(42, "x")


def test_interpolate_colormap_returns_256_entries():
    stops = [[0, 0, 0, 255], [255, 255, 255, 255]]
    lut = interpolate_colormap(stops)
    assert len(lut) == 256
    assert lut[0] == [0, 0, 0, 255]
    assert lut[-1] == [255, 255, 255, 255]
    # Midpoint should be roughly gray.
    mid = lut[128]
    assert 120 <= mid[0] <= 140


def test_interpolate_colormap_multi_stop():
    stops = [[0, 0, 0, 255], [255, 0, 0, 255], [255, 255, 255, 255]]
    lut = interpolate_colormap(stops)
    assert lut[0] == [0, 0, 0, 255]
    assert lut[-1] == [255, 255, 255, 255]


def test_build_categorical_lut_maps_values_to_slots():
    """Values 1, 2, 3 over range [1, 3] should land in slot 0, 128, 255."""
    cats = {1: [10, 20, 30, 255], 2: [40, 50, 60, 255], 3: [70, 80, 90, 255]}
    lut = build_categorical_lut(cats, (1, 3))
    assert len(lut) == 256
    assert lut[0] == [10, 20, 30, 255]
    assert lut[128] == [40, 50, 60, 255]
    assert lut[255] == [70, 80, 90, 255]
    # Unassigned slots stay transparent.
    assert lut[1] == [0, 0, 0, 0]


def test_build_categorical_lut_zero_range_does_not_divide_by_zero():
    cats = {5: [255, 0, 0, 255]}
    # lo == hi — span fallback kicks in (or 1.0).
    lut = build_categorical_lut(cats, (5, 5))
    assert any(entry == [255, 0, 0, 255] for entry in lut)


def test_build_categorical_lut_out_of_range_values_skipped():
    cats = {10: [255, 0, 0, 255], 999: [0, 255, 0, 255]}
    lut = build_categorical_lut(cats, (0, 10))
    # 10 maps to slot 255; 999 is outside [0, 10] but slot computation may still
    # land it >255 → silently dropped.
    assert lut[255] == [255, 0, 0, 255]
    # 999 should NOT appear anywhere.
    assert [0, 255, 0, 255] not in lut
