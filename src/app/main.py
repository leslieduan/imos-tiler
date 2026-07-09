import asyncio
import traceback
from contextlib import asynccontextmanager

import anyio
import starlette.middleware.gzip as _gzip_mw
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app.config import settings
from app.routers.data_tiles import router as data_tiles_router
from app.routers.visual_tiles import router as visual_tiles_router
from app.services.colormap.registry import load_colormaps
from app.services.product.registry import iter_products, load_products
from app.services.rendering.kernels import warmup_resample
from app.services.rendering.visual_tiles import warmup_visual
from app.services.store.registry import prewarm_stores


@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = settings.THREAD_POOL_SIZE
    print(f"Thread pool size set: {limiter.total_tokens}")
    load_products()
    load_colormaps()
    print(
        "Cache configured:",
        {
            "cache_backend": settings.CACHE_BACKEND,
            "slice_cache_ttl_seconds": settings.SLICE_CACHE_TTL_SECONDS,
            "processed_cache_ttl_seconds": settings.PROCESSED_CACHE_TTL_SECONDS,
            "store_ttl_seconds": settings.STORE_TTL_SECONDS,
        },
    )

    await anyio.to_thread.run_sync(warmup_resample)
    await anyio.to_thread.run_sync(warmup_visual)
    store_urls = list({p.source_path for p in iter_products()})
    store_prewarm_task = asyncio.create_task(prewarm_stores(store_urls))
    yield
    print("Shutting down")
    store_prewarm_task.cancel()
    try:
        await store_prewarm_task
    except asyncio.CancelledError:
        pass
    except Exception:
        print("Background task exited with error")
        traceback.print_exc()


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

# GZipMiddleware's only content-type control is this module-level deny-list, which by
# default excludes just text/event-stream. Extend it to skip image/* so already-compressed
# PNG/GIF/WebP/APNG tiles aren't re-gzipped (pure CPU waste on the hot tile path) — JSON
# responses (manifest, listings) still compress. The deny-list is read by name
# at request time, so a Starlette upgrade that renames/inlines it would silently disable
# this exclusion; test_main.py::test_gzip_skips_image_tiles fails loudly if that happens.
if "image/" not in _gzip_mw.DEFAULT_EXCLUDED_CONTENT_TYPES:
    # Starlette types the constant as a 1-tuple; widening it trips mypy's assignment check.
    _gzip_mw.DEFAULT_EXCLUDED_CONTENT_TYPES += ("image/",)  # type: ignore[assignment]
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)

app.include_router(data_tiles_router, prefix="/data_tiles", tags=["data_tiles"])
app.include_router(visual_tiles_router, prefix="/visual_tiles", tags=["visual_tiles"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    print(f"Unhandled error: method={request.method} path={request.url.path}")
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
def health():
    return {"status": "ok"}
