import asyncio
import logging
import os
from contextlib import asynccontextmanager

import anyio
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.constants import DISK_CACHE_PATH, PRODUCTS, Product
from app.log_config import configure_logging
from app.routers.admin import admin_router
from app.routers.data_tiles import router as data_tiles_router
from app.routers.visual_tiles import router as visual_tiles_router
from app.services.colormap_config import load_colormaps
from app.services.disk_cache import (
    evict_stale_and_orphans,
    prewarm_disk_slices,
    refresh_disk_cache,
)
from app.services.product_config import load_products
from app.services.store_registry import prewarm_stores

load_dotenv()
configure_logging()

logger = logging.getLogger(__name__)

# Thinking on pixel drill: currently, to make tiles response fast, we cache Zarr slices per date per variable on disk. Because current chuking shape is (5 times, full_grid), this is good for map visualisation.
# But not good for pixel drill, chuking like (full_time, small_grid) would be good. Even though we have this chunking Zarr, we still will face tricky chanlledge in how to cache the zarr slice on disk. Becasue
# the cache on disk for tiles visualisation cannot be used for pixel drill, so we might need to cache a duplciate zarr slice on disk for pixel drill, the cache will be like full time per variable. It seems
# impossible that  we can share the cache on disk between pixel drill and tiles visualisation. Because if tiles use the full time per variable cache, the response will be too slow, it will need read full grid.
# Also memory cache is enabled for tiles, because lods change, there will be new request to the same slice, so the slice cache can be shared. But for pixel drill, it will be very unlikely that there are requests
# sharing the same slice. Even if the chunking is (full_time, small_grid), the hit rate of memory cache will still be very low, as there are too many small grids. So it is not worth to enable memory cache for pixel drill.
# So the cache strategy for pixel drill is only cache on disk, and the cache strategy for tiles visualisation is cache on disk and in memory.


async def _startup_cache_sync(products: list[Product]) -> None:
    # Evict stale dates and orphan product dirs first so the cache reflects the current
    # product/date state from the moment the server starts serving, then prewarm in
    # parallel. Both run in threads so the event loop stays free for requests.
    try:
        await asyncio.to_thread(evict_stale_and_orphans, products)
    except Exception:
        logger.exception("Startup cache eviction failed; continuing to prewarm")
    await asyncio.to_thread(prewarm_disk_slices, products)


async def _cache_refresh_loop(interval: int) -> None:
    while True:
        # yields to event loop — other tasks/requests run freely during the wait
        await asyncio.sleep(interval)
        # Re-read PRODUCTS each cycle so products added/removed via the admin API are reflected.
        # Catch broadly: an unhandled exception here would kill the loop for the lifetime
        # of the process, silently disabling all future refreshes.
        try:
            await asyncio.to_thread(refresh_disk_cache, list(PRODUCTS.values()))
        except Exception:
            logger.exception("Cache refresh cycle failed; will retry next interval")


# Lifespan manages server startup and shutdown. Everything before yield runs on startup,
# everything after yield runs on shutdown. The server handles requests while paused at yield.
@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = int(os.environ.get("THREAD_POOL_SIZE", 100))
    logger.info("Thread pool size set", extra={"thread_pool_size": limiter.total_tokens})
    load_products()
    load_colormaps()
    logger.info(
        "Disk cache enabled",
        extra={
            "path": DISK_CACHE_PATH,
            "limit_gb": int(os.environ.get("DISK_CACHE_LIMIT_GB", 20)),
            "days": int(os.environ.get("CACHE_DAYS", 30)),
            "workers": int(os.environ.get("PREWARM_WORKERS", 8)),
        },
    )
    logger.info(
        "Memory cache configured",
        extra={
            "slice_cache_size": int(os.environ.get("SLICE_CACHE_SIZE", 10)),
            "processed_cache_size": int(os.environ.get("PROCESSED_CACHE_SIZE", 50)),
            "store_ttl_seconds": int(os.environ.get("STORE_TTL_SECONDS", 600)),
        },
    )
    store_urls = list({p.source_path for p in PRODUCTS.values()})
    prewarm_stores(store_urls)
    prewarm_task = asyncio.create_task(_startup_cache_sync(list(PRODUCTS.values())))
    interval = int(os.environ.get("CACHE_REFRESH_INTERVAL_SECONDS", 14400))
    logger.info("Cache refresh interval set", extra={"interval_seconds": interval})
    refresh_task = asyncio.create_task(_cache_refresh_loop(interval))
    yield
    logger.info("Shutting down")
    for task in (prewarm_task, refresh_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background task exited with error")


app = FastAPI(
    title="IMOS Tile Server",
    description="On-demand RGBA PNG tiles for IMOS ocean data products, served from Zarr stores on S3.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(data_tiles_router, prefix="/data_tiles", tags=["data_tiles"])
app.include_router(visual_tiles_router, prefix="/visual_tiles", tags=["visual_tiles"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled error",
        extra={"method": request.method, "path": request.url.path},
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
def health():
    return {"status": "ok"}
