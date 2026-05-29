"""Build the committed land-mask asset used by coastal fill (data/land_mask.npz).

Dev-only, run once (regenerate only if you change resolution or the coastline
source). NOT part of the project's runtime/dev dependencies — invoke with uv's
ephemeral deps so nothing leaks into the environment:

    uv run --with regionmask --with cartopy --with pooch \
        python scripts/build_land_mask.py

Coastline source: Natural Earth 1:10m land polygons, via
``regionmask.defined_regions.natural_earth_v5_0_0.land_10``.
Natural Earth data is public domain (https://www.naturalearthdata.com/about/terms-of-use/).

Output: a global bit-packed boolean grid (True = land), north→south, covering
[-180, 180) lon x (90, -90] lat at ``RES`` degrees. Stored packed (~3 MB) so it
costs little in git; the runtime unpacks it once in coastal.load_land_mask().
"""

import numpy as np
import regionmask

RES = 0.05  # ~5.5 km cells — finer than any current render grid
LON_MIN = -180.0
LAT_MAX = 90.0
OUT = "data/land_mask.npz"


def main() -> None:
    # Cell centres on a regular global grid, north→south to match the render grid
    # orientation (linspace(lat_max, lat_min, h) in kernels.resample_variables_to_grid).
    lon = np.arange(LON_MIN, 180.0, RES) + RES / 2.0
    lat = np.arange(LAT_MAX, -90.0, -RES) - RES / 2.0

    region = regionmask.defined_regions.natural_earth_v5_0_0.land_10
    # mask() returns the region number (0) over land, NaN over ocean.
    mask = region.mask(lon, lat)
    land = mask.notnull().values  # bool, shape (lat, lon), True = land

    packed = np.packbits(land)  # flatten C-order + pack to bits
    np.savez_compressed(
        OUT,
        packed=packed,
        shape=np.array(land.shape, dtype=np.int64),
        res=np.float64(RES),
        lon_min=np.float64(LON_MIN),
        lat_max=np.float64(LAT_MAX),
    )
    print(
        f"wrote {OUT}: grid {land.shape} ({land.mean() * 100:.1f}% land), "
        f"packed {packed.nbytes / 1e6:.2f} MB"
    )


if __name__ == "__main__":
    main()
