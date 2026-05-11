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
app.include_router(admin_router, prefix="/admin", tags=["Admin"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/")
def read_index():
    return {"message": "Welcome to TiTiler"}
