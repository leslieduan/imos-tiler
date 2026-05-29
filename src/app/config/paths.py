"""On-disk file/directory paths the server reads or writes at runtime.

These are deliberately *not* env vars by default — they're hard-coded relative
to the working directory the server is launched from. Override at deploy time
via the existing Docker mount/volume conventions if needed.

Separated from [[constants]] (which holds the server↔shader contract values)
because paths are operational config, not shader-coupled invariants. Changing a
path doesn't risk silently corrupting tile output.
"""

from pathlib import Path

PRODUCTS_CONFIG_PATH = "data/products.json"
COLORMAPS_CONFIG_PATH = "data/colormaps.json"
DISK_CACHE_PATH = "slice_cache"
# Committed global land-mask asset for coastal fill (see services/rendering/coastal.py).
# Unlike the paths above, this is a *static* asset shipped with the package, not
# runtime state — so it's resolved relative to the package (CWD-independent) and
# lives outside the runtime `data/` dir, which the service may own/write (a
# service-owned data/ would otherwise block `git pull` of the asset).
# Regenerate with scripts/build_land_mask.py.
LAND_MASK_PATH = str(Path(__file__).resolve().parents[1] / "assets" / "land_mask.npz")
