"""App configuration constants.

Local-dev only: no Docker, no .env, no env-var overrides. To change a value,
edit it here and restart the server — same pattern as ``products.json`` /
``colormaps.json`` in this package.
"""

# All date strings exposed by the API are local dates in this timezone (see
# app/utils/dates.py). Zarr store timestamps are always UTC.
TILE_TIMEZONE = "Australia/Sydney"

# Seconds before a Zarr store is re-opened to pick up newly appended time steps.
STORE_TTL_SECONDS = 600

# Capacity gate for concurrent store opens during startup prewarm. Bounded to
# the S3 connection ceiling, not CPU.
STORE_PREWARM_WORKERS = 8

# Max concurrent sync route handlers (each cold slice needs one thread).
# Practical ceiling is S3 bandwidth — beyond ~150 concurrent .compute() calls
# you are likely to hit network saturation before gaining further throughput.
THREAD_POOL_SIZE = 100

# Capacity gate for /animation per-frame S3 fan-out. Sized to the aiobotocore
# S3 connection-pool ceiling (~10/host) — going higher just queues on the pool.
ANIMATION_WORKERS = 10

# Cache backend for L1 (processed grid) / L2 (slice): "none" disables caching
# entirely; "redis" shares a cache across instances via REDIS_URL.
CACHE_BACKEND = "none"

# Only used when CACHE_BACKEND = "redis".
REDIS_URL = ""
REDIS_LOCK_TTL_SECONDS = 30
REDIS_WAIT_TIMEOUT_SECONDS = 15

# Per-entry TTL in seconds, only used when CACHE_BACKEND = "redis" (the "none"
# backend recomputes on every call, so TTL is irrelevant there).
SLICE_CACHE_TTL_SECONDS = 600
PROCESSED_CACHE_TTL_SECONDS = 600

# S3_ANON=True uses anonymous access for s3:// URLs — correct for IMOS's public
# AODN buckets. Set False to let fsspec discover AWS credentials for private
# buckets via env vars, ~/.aws/credentials, or IAM role.
S3_ANON = True

# Per-socket S3 timeouts (botocore). connect_timeout bounds DNS + TCP/TLS
# handshake; read_timeout is per-read inactivity, not total request duration —
# zarr chunks are MB-sized so 30s has lots of headroom.
S3_CONNECT_TIMEOUT = 5
S3_READ_TIMEOUT = 30
S3_MAX_ATTEMPTS = 2
