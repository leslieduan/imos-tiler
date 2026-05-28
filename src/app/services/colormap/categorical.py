"""Categorical (flag-valued) rendering schemes.

Distinct from the continuous colormap pipeline ([[colormap.resolver]]): a
continuous variable is a scalar field mapped through a 0–255 ramp, but a
categorical variable (CF ``flag_values``) is a set of discrete integer codes
that must map *exactly* to a colour — interpolating between codes invents
categories that aren't in the data.

Callers first gate on :func:`is_categorical_variable`; only then do they call
:func:`resolve_scheme`, which assembles a :class:`CategoricalScheme` from three
colour sources, by precedence:

  1. an explicit categorical ``colormap=`` query param (advanced opt-in),
  2. the variable's ``flag_colors`` attribute, if the store ever carries one
     (not CF-standard; absent on every current product),
  3. the built-in :data:`DEFAULT_CATEGORICAL_PALETTE`.

The category *values* and *labels* always come from the data (``flag_values`` /
``flag_meanings``); only the *colours* follow the precedence above. ``lut()``
produces a full 256-entry table indexed by the raw integer code so rio-tiler's
``apply_cmap`` takes its fast ``make_lut`` path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.colormap.registry import get_colormap, is_categorical
from app.utils.colors import categorical_slot, parse_color

RGBA = tuple[int, int, int, int]

# Built-in default palette, aligned by position to the variable's flag_values.
# Marine cold-spell categories follow the Hobday et al. (2018) convention: a
# light→dark cold-blue ramp, with category 0 ("none") transparent so the
# basemap shows through. Cycled if a variable has more categories than colours.
DEFAULT_CATEGORICAL_PALETTE: list[RGBA] = [
    (0, 0, 0, 0),  # 0 none — transparent
    (199, 236, 242, 255),  # 1 moderate  #C7ECF2
    (133, 183, 204, 255),  # 2 strong    #85B7CC
    (74, 111, 167, 255),  # 3 severe    #4A6FA7
    (17, 30, 108, 255),  # 4 extreme   #111E6C
]


def is_categorical_variable(attrs: Mapping[str, Any]) -> bool:
    """True if a variable is categorical, i.e. it declares CF ``flag_values``."""
    return attrs.get("flag_values") is not None


@dataclass(frozen=True)
class CategoricalScheme:
    """Discrete value→colour mapping for one categorical variable."""

    values: tuple[int, ...]
    colors: tuple[RGBA, ...]
    labels: tuple[str, ...] | None

    def lut(self) -> dict[int, RGBA]:
        """256-entry RGBA LUT indexed by the raw integer code; unmapped → transparent.

        Codes outside 0–255 are skipped (rio-tiler's uint8 LUT can't represent
        them) — categorical products use small non-negative codes in practice.
        """
        table: dict[int, RGBA] = {i: (0, 0, 0, 0) for i in range(256)}
        for value, color in zip(self.values, self.colors, strict=False):
            if 0 <= value <= 255:
                table[value] = color
        return table


def parse_flag_values_and_meanings(
    attrs: Mapping[str, Any],
) -> tuple[tuple[int, ...], tuple[str, ...] | None]:
    """Return ``(values, labels)`` straight from a categorical variable's attrs.

    ``values`` are the CF ``flag_values`` coerced to ints; ``labels`` are the
    matching ``flag_meanings`` aligned 1:1, or None when absent or misaligned.
    Callers must gate on :func:`is_categorical_variable` first. Colour-free, so
    the manifest can surface the raw categories without touching the colormap
    registry.
    """
    values = tuple(_as_int_list(attrs.get("flag_values")))
    labels = _parse_meanings(attrs.get("flag_meanings"), len(values))
    return values, labels


def resolve_scheme(attrs: Mapping[str, Any], colormap_name: str | None) -> CategoricalScheme:
    """Build a scheme for a categorical variable's attrs.

    Assumes the variable is categorical — callers must gate on
    :func:`is_categorical_variable` first. Values and labels come from the data;
    colours follow the precedence documented in the module docstring.
    """
    values, labels = parse_flag_values_and_meanings(attrs)
    colors = _resolve_colors(values, attrs, colormap_name)
    return CategoricalScheme(values=values, colors=colors, labels=labels)


def _resolve_colors(
    values: tuple[int, ...],
    attrs: Mapping[str, Any],
    colormap_name: str | None,
) -> tuple[RGBA, ...]:
    n = len(values)

    # Rule 1 (precedence) — explicit categorical colormap param wins. The render
    # path rejects a categorical colormap whose values don't match the variable's
    # flag_values before reaching here (see _validate_categorical_request in
    # [[rendering.visual_tiles]]), so by here the values align and we read its
    # colour at each value's slot directly.
    if colormap_name and is_categorical(colormap_name):
        explicit = _registered_categorical_colors(colormap_name, values)
        if explicit:
            return tuple(explicit)

    # Rule 2 — colours carried by the data itself (not CF-standard; best-effort).
    flag_colors = attrs.get("flag_colors")
    if flag_colors:
        parsed = _parse_flag_colors(flag_colors)
        if parsed:
            return _fit(parsed, n)

    # Rule 3 — the built-in default palette.
    return _fit(DEFAULT_CATEGORICAL_PALETTE, n)


def _registered_categorical_colors(name: str, values: tuple[int, ...]) -> list[RGBA]:
    """Colour per category value from a registered categorical colormap's 256-LUT.

    Categorical colormaps are stored with each value's colour at the slot
    ``categorical_slot(value, min, max)`` (see [[utils.colors]]). The request-time
    match check enforces that the colormap's values equal the product's flag_values,
    so indexing by that slot recovers the right colour for every value — including
    transparent ones, which a scan for "non-empty entries" would drop.
    """
    lut = get_colormap(name)
    if not lut or not values:
        return []
    lo, hi = min(values), max(values)
    return [tuple(lut[categorical_slot(v, lo, hi)]) for v in values]  # type: ignore[misc]


def _parse_flag_colors(raw: Any) -> list[RGBA]:
    """Best-effort parse of a ``flag_colors`` attr: a list of, or space-separated,
    hex strings / [r,g,b,a] entries. Unparseable entries are skipped."""
    items: Sequence[Any]
    if isinstance(raw, str):
        items = raw.split()
    elif isinstance(raw, Sequence):
        items = raw
    else:
        return []
    out: list[RGBA] = []
    for item in items:
        try:
            r, g, b, a = parse_color(item, "flag_colors")
            out.append((r, g, b, a))
        except ValueError:
            continue
    return out


def _fit(colors: Sequence[RGBA], n: int) -> tuple[RGBA, ...]:
    """Return exactly n colours, cycling the source palette if it is shorter."""
    if not colors:
        colors = DEFAULT_CATEGORICAL_PALETTE
    return tuple(colors[i % len(colors)] for i in range(n))


def _parse_meanings(raw: Any, n: int) -> tuple[str, ...] | None:
    """Parse flag_meanings into per-value labels, or None if absent/misaligned."""
    if raw is None:
        return None
    labels = raw.split() if isinstance(raw, str) else [str(x) for x in raw]
    if len(labels) != n:
        return None  # misaligned with flag_values — drop rather than mispair
    return tuple(labels)


def _as_int_list(raw: Any) -> list[int]:
    """Coerce a flag_values attr (numpy array, list, or scalar) to a list of ints."""
    if raw is None or isinstance(raw, str | bytes):
        return []
    values = raw.tolist() if hasattr(raw, "tolist") else raw
    if not isinstance(values, list | tuple):
        values = [values]
    out: list[int] = []
    for v in values:
        try:
            out.append(int(v))
        except (ValueError, TypeError):
            continue
    return out
