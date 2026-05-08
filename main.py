import logging.config

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from uvicorn.config import LOGGING_CONFIG

from routers.tiles import router as tiles_router

LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(levelprefix)s %(asctime)s %(message)s"
LOGGING_CONFIG["formatters"]["default"]["datefmt"] = "%H:%M:%S"
LOGGING_CONFIG["loggers"]["services"] = {
    "handlers": ["default"],
    "level": "INFO",
    "propagate": False,
}
logging.config.dictConfig(LOGGING_CONFIG)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tiles_router, prefix="/tiles", tags=["Tiles"])


@app.get("/")
def read_index():
    return {"message": "Welcome to TiTiler"}
