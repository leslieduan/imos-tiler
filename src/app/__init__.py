"""Load .env before any submodule is imported.

Several config modules read env vars at *module load* time (e.g.
``config/paths.py``'s ``DISK_CACHE_PATH`` and ``caching/slice_cache.py``'s
``SLICE_CACHE_SIZE``). Python runs this package ``__init__`` before importing any
``app.*`` submodule, so calling ``load_dotenv()`` here guarantees the environment
is populated before those reads — ``main.py``'s own ``load_dotenv()`` runs only
*after* its ``from app.config... import`` lines and is too late for them.

``load_dotenv()`` does not override variables already set in the real
environment, so shell/Docker overrides still win over ``.env``.
"""

from dotenv import load_dotenv

load_dotenv()
