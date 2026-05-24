"""On-disk file/directory paths the server reads or writes at runtime.

These are deliberately *not* env vars by default — they're hard-coded relative
to the working directory the server is launched from. Override at deploy time
via the existing Docker mount/volume conventions if needed.

Separated from [[constants]] (which holds the server↔shader contract values)
because paths are operational config, not shader-coupled invariants. Changing a
path doesn't risk silently corrupting tile output.
"""

PRODUCTS_CONFIG_PATH = "data/products.json"
COLORMAPS_CONFIG_PATH = "data/colormaps.json"
DISK_CACHE_PATH = "slice_cache"
