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
from routers.admin import router as admin_router
from routers.raster import router as raster_router
from routers.tiles import router as tiles_router
from services.loader import prewarm_stores
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

# TODO: proactive poll, detects if zarr updated. (quite overskill but a smart plan if needed in the future)
# A background thread wakes every N minutes(cron job), calls xr.open_zarr for each unique store URL,
# and compares ds.sizes["time"] against the cached store. If the shape has grown (new time
# steps appended), evict the store entry and call prewarm_stores so the cache is refreshed
# before any request arrives. Reading .zmetadata via xr.open_zarr is pure metadata — no
# spatial data chunks are fetched — so the poll is cheap even across many stores.
# This would make the TTL a pure safety net (poll thread crash) rather than the primary
# refresh mechanism. Not implemented because IMOS data updates at most daily and the
# stale-while-revalidate TTL already ensures no request ever blocks on a re-open.


@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = int(os.environ.get("THREAD_POOL_SIZE", 100))
    load_products()
    store_urls = list({p.source_path for p in PRODUCTS.values()})
    prewarm_stores(store_urls)
    yield


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

app.include_router(tiles_router, prefix="/tiles", tags=["Tiles"])
app.include_router(raster_router, prefix="/raster", tags=["Raster"])
app.include_router(admin_router, prefix="/admin", tags=["Admin"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/")
def read_index():
    return {"message": "Welcome to TiTiler"}
