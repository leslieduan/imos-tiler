from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from titiler.core.factory import TilerFactory

from routers.netcdf_tiles import router as tiles_router
from routers.zarr_tiles import router as zarr_router

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
app.include_router(tiles_router, prefix="/tiles/netcdf", tags=["NetCDF Tiles"])
app.include_router(zarr_router, prefix="/tiles/zarr", tags=["Zarr Tiles"])


@app.get("/")
def read_index():
    return {"message": "Welcome to TiTiler"}