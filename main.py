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

from constants import PRODUCTS
from routers.admin import admin_router
from routers.data_tiles import router as data_tiles_router
from routers.visual_tiles import router as visual_tiles_router
from services.colormap_store import load_colormaps
from services.loader import prewarm_disk_slices, prewarm_stores, refresh_disk_cache
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


async def _cache_refresh_loop(products: list, interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(refresh_disk_cache, products)


@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = int(os.environ.get("THREAD_POOL_SIZE", 100))
    load_products()
    load_colormaps()
    store_urls = list({p.source_path for p in PRODUCTS.values()})
    prewarm_stores(store_urls)
    products = list(PRODUCTS.values())
    asyncio.create_task(asyncio.to_thread(prewarm_disk_slices, products))
    interval = int(os.environ.get("CACHE_REFRESH_INTERVAL_SECONDS", 14400))
    refresh_task = asyncio.create_task(_cache_refresh_loop(products, interval))
    yield
    refresh_task.cancel()
    try:
        await refresh_task
    except asyncio.CancelledError:
        pass


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


@app.get("/")
def read_index():
    return {"message": "Welcome to TiTiler"}
