from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from titiler.core.factory import TilerFactory

from routers.tiles import router as tiles_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

cog = TilerFactory()
app.include_router(cog.router, tags=["Cloud Optimized GeoTIFF"])
app.include_router(tiles_router, prefix="/tiles", tags=["Tiles"])


@app.get("/")
def read_index():
    return {"message": "Welcome to TiTiler"}