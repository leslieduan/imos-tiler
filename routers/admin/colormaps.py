"""Admin CRUD for custom colormaps."""

import logging

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from services.colormap_config import ColormapMode, register_colormap, remove_colormap
from utils.colors import build_categorical_lut, interpolate_colormap, parse_color

logger = logging.getLogger(__name__)


router = APIRouter()


class ColormapPayload(BaseModel):
    name: str
    mode: ColormapMode = Field(
        default="ramp",
        description=(
            "'ramp': evenly-spaced stops, linearly interpolated to 256 LUT entries. Dataset-agnostic. "
            "'categorical': discrete integer value→color mapping (equivalent to CF flag_values+flag_colors). "
            "Dataset-specific — the entry keys must exactly match the integer values present in the dataset. "
            "Applying a categorical colormap to a dataset with different values renders without error "
            "but produces silently wrong colours. Name categorical colormaps after the dataset or variable "
            "they describe to make the coupling explicit."
        ),
    )
    entries: list[list[int]] = Field(
        ...,
        description=(
            "ramp mode — 2–256 color stops (hex string or [r,g,b,a] list), interpolated to 256. "
            "categorical mode — dict mapping integer data values to colors, "
            'e.g. {"1": "#ff0000", "2": [0, 0, 255, 255]}. '
            "Keys must match the exact integer values in the target dataset variable."
        ),
    )

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v or v != v.strip():
            raise ValueError("must be non-empty with no leading/trailing whitespace")
        return v

    @model_validator(mode="before")
    @classmethod
    def build_lut(cls, data: dict) -> dict:
        mode = data.get("mode", "ramp")
        raw = data.get("entries")
        if isinstance(raw, dict) and mode != "categorical":
            raise ValueError(
                f'entries is a dict, which is only valid for mode="categorical" '
                f'(current mode: {mode!r}). Set "mode": "categorical" or pass a list of colour stops.'
            )
        if mode != "categorical":
            return data
        raw = data.get("entries")
        if not isinstance(raw, dict):
            raise ValueError('entries must be a dict for categorical mode, e.g. {"1": "#ff0000"}')
        categories: dict[int, list[int]] = {}
        for k, v in raw.items():
            try:
                val = int(k)
            except (ValueError, TypeError) as e:
                raise ValueError(f"entries key {k!r} must be an integer") from e
            categories[val] = parse_color(v, f"entries[{k!r}]")
        data_range = (float(min(categories)), float(max(categories)))
        data["entries"] = build_categorical_lut(categories, data_range)
        return data

    @field_validator("entries", mode="before")
    @classmethod
    def entries_valid(cls, v: list) -> list[list[int]]:
        if len(v) < 2:
            raise ValueError(f"entries must have at least 2 color stops, got {len(v)}")
        if len(v) > 256:
            raise ValueError(f"entries must have at most 256 items, got {len(v)}")
        normalized: list[list[int]] = []
        for i, entry in enumerate(v):
            try:
                rgba = parse_color(entry, f"entry {i}")
            except ValueError as e:
                raise ValueError(f"entry {i}: {e}") from e
            normalized.append(rgba)
        return normalized if len(normalized) == 256 else interpolate_colormap(normalized)

    def to_tuples(self) -> list[tuple[int, int, int, int]]:
        return [(rgba[0], rgba[1], rgba[2], rgba[3]) for rgba in self.entries]


@router.post(
    "/colormaps",
    status_code=201,
    summary="Register a custom colormap",
    description="Registers a named colormap from 2–256 color stops, interpolated to 256 entries. Returns 409 if the name already exists.",
)
def add_colormap(payload: ColormapPayload):
    try:
        register_colormap(payload.name, payload.to_tuples(), payload.mode)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to persist colormap %s", payload.name)
        raise HTTPException(status_code=500, detail=f"Failed to persist colormap: {e}") from e
    logger.info("Colormap registered: %s (%s)", payload.name, payload.mode)
    return JSONResponse(status_code=201, content={"name": payload.name})


@router.delete(
    "/colormaps/{name}",
    status_code=204,
    summary="Remove a custom colormap",
    description="Removes a previously registered custom colormap. Returns 404 if not found.",
)
def delete_colormap(name: str = Path(...)):
    try:
        remove_colormap(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Colormap '{name}' not found") from e
    except Exception as e:
        logger.exception("Failed to persist removal of colormap %s", name)
        raise HTTPException(status_code=500, detail=f"Failed to persist removal: {e}") from e
    logger.info("Colormap removed: %s", name)
