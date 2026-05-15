import asyncio
import logging
import logging.config
import os
from contextlib import asynccontextmanager

import anyio
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from uvicorn.config import LOGGING_CONFIG

from constants import PRODUCTS, Product
from routers.admin import admin_router
from routers.data_tiles import router as data_tiles_router
from routers.visual_tiles import router as visual_tiles_router
from services.colormap_store import load_colormaps
from services.loader import (
    evict_stale_and_orphans,
    prewarm_disk_slices,
    prewarm_stores,
    refresh_disk_cache,
)
from services.product_store import load_products

logger = logging.getLogger(__name__)

load_dotenv()

LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(levelprefix)s %(asctime)s %(message)s"
LOGGING_CONFIG["formatters"]["default"]["datefmt"] = "%H:%M:%S"
LOGGING_CONFIG["loggers"]["services"] = {
    "handlers": ["default"],
    "level": "INFO",
    "propagate": False,
}
logging.config.dictConfig(LOGGING_CONFIG)


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
#
# prewarm_task and refresh_task are async tasks scheduled on the event loop via create_task.
# They run concurrently with incoming requests — pausing at await points so the event loop
# stays free to handle requests. They are NOT blocked by each other or by request handling.
#
# The only blocking risk is CPU/IO-heavy work inside these tasks. That's why prewarm_disk_slices
# and refresh_disk_cache are wrapped in asyncio.to_thread — offloading them to a thread pool
# so the event loop is never frozen.
@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = int(os.environ.get("THREAD_POOL_SIZE", 100))
    load_products()
    load_colormaps()
    store_urls = list({p.source_path for p in PRODUCTS.values()})
    prewarm_stores(store_urls)
    prewarm_task = asyncio.create_task(_startup_cache_sync(list(PRODUCTS.values())))
    interval = int(os.environ.get("CACHE_REFRESH_INTERVAL_SECONDS", 14400))
    refresh_task = asyncio.create_task(_cache_refresh_loop(interval))
    yield
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
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
def health():
    return {"status": "ok"}


# TODO: 1. Coordinate system and projection pipeline section in technical.md is not good, because we
# have two different tiles systems, the data-tiles is using custom xyz tiles system, while the visual-tiles is using standard web mercator.
# We should clarify this in the documentation, specify data-tiles is genreated in ESGF:4326 and visual-tiles is generate in Web Mercator:3857,
# and webgl shader will reproject data-tiles from ESGF:4326 to Web Mercator:3857. While visual-tiles is already in Web Mercator:3857, so no
# reprojection is needed, and it work well with all the map library or services that support web mercator.

# TODO: 2. Background task including prewarm_task and refresh_task is not accurately described in technical.md.


# TODO:3. We should have a section in technical.md to explain how the event loop and multithreading work in this project,
# so when background taks running CPU/IO-heavy work, it will not block the event loop and the server can still handle incoming requests concurrently.
# Also, explain why not use event loop for most endpoints, because of heavy CPU/IO work and xarray not supoort async reading.
