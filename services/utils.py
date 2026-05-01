import math

from constants import MAX_LODS, MAX_VIRTUAL_CHUNKS


def compute_lod_grids(
    data_width: int,
    data_height: int,
    chunk_px: tuple[int, int],
) -> dict[int, tuple[int, int]]:
    """
    Derive LOD grids from data dimensions and chunk size.

    LOD 1 is the coarsest (fewest tiles); the highest LOD is the finest.
    Each finer level doubles the grid until the finest level covers the full
    data extent (total_px ≥ data dimensions).

    Constraints enforced:
    - grid_cols × grid_rows ≤ MAX_VIRTUAL_CHUNKS at every level
    - At most MAX_LODS levels total
    - Minimum 1×1 grid
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

    # Build levels from finest downward, halving each time
    levels = [(finest_cols, finest_rows)]
    while True:
        cols = max(1, levels[-1][0] // 2)
        rows = max(1, levels[-1][1] // 2)
        if (cols, rows) == levels[-1]:
            break
        levels.append((cols, rows))

    # Reverse so index 0 = coarsest, then cap at MAX_LODS
    levels.reverse()
    levels = levels[-MAX_LODS:]

    return {i + 1: level for i, level in enumerate(levels)}
