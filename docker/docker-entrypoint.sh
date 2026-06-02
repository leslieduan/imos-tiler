#!/bin/sh
set -e

mkdir -p /app/data "${DISK_CACHE_PATH:-/app/slice_cache}"

[ -f /app/data/products.json ]  || echo '[]' > /app/data/products.json
[ -f /app/data/colormaps.json ] || echo '{}' > /app/data/colormaps.json

exec "$@"
