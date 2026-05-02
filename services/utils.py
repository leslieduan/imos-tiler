import math

from constants import MAX_LODS, MAX_VIRTUAL_CHUNKS, MIN_COARSEST_GRID


def compute_lod_grids(
    data_width: int,
    data_height: int,
    chunk_px: tuple[int, int],
    max_lods: int = MAX_LODS,
    min_coarsest: tuple[int, int] = MIN_COARSEST_GRID,
) -> dict[int, tuple[int, int]]:
    """
    Derive LOD grids from data dimensions and chunk size.

    LOD 1 is coarsest (fewest tiles); the highest LOD is finest (one chunk per
    native data chunk). Each level doubles the grid via ceil(finest / 2^k), so
    coverage is never under-counted at intermediate scales.

    Constraints:
    - finest grid is clamped so cols × rows ≤ MAX_VIRTUAL_CHUNKS
    - levels whose (cols, rows) fall below min_coarsest are dropped
    - at most max_lods levels are returned (the finest end is kept)

    Example:
        compute_lod_grids(3000, 1500, (256, 256))
        # → {1: (3, 2), 2: (6, 3), 3: (12, 6)}
    """
    cw, ch = chunk_px

    finest_cols = max(1, math.ceil(data_width / cw))
    finest_rows = max(1, math.ceil(data_height / ch))

    # Clamp finest level to fit within MAX_VIRTUAL_CHUNKS
    while finest_cols * finest_rows > MAX_VIRTUAL_CHUNKS:
        if finest_cols >= finest_rows:
            finest_cols = max(1, finest_cols - 1)
        else:
            finest_rows = max(1, finest_rows - 1)

    # Depth: halvings until both axes reach 1
    max_depth = math.floor(math.log2(max(finest_cols, finest_rows))) if max(finest_cols, finest_rows) > 1 else 0

    levels = []
    for k in range(max_depth + 1):
        scale = 2 ** k
        levels.append((max(1, math.ceil(finest_cols / scale)), max(1, math.ceil(finest_rows / scale))))

    # Reverse: coarsest → finest; drop levels below min_coarsest; cap at max_lods
    levels.reverse()
    min_cols, min_rows = min_coarsest
    levels = [lvl for lvl in levels if lvl[0] >= min_cols and lvl[1] >= min_rows]

    return {i + 1: lvl for i, lvl in enumerate(levels[-max_lods:])}
