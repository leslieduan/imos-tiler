"""Build the committed ocean-mask asset (src/app/assets/ocean_mask.npz).

Dev-only, run once (regenerate only if the source OCmask changes). The netCDF
reader is NOT a project runtime/dev dependency — invoke with uv's ephemeral deps
so nothing leaks into the environment:

    uv run --with h5netcdf --with h5py python scripts/build_ocean_mask.py

Source: src/app/assets/OCmask.nc — an HDF5/netCDF4 grid with an ``OCmask``
variable on (lat, lon) where 1.0 = valid ocean and NaN = invalid. The model
sea-level product (UCUR/VCUR currents) carries anomalous points outside this
mask; we use it to cut them off at render time (see coastal.ocean_mask_for_grid).

Output: a regional bit-packed boolean grid (True = valid ocean), north→south,
mirroring the land_mask.npz format so the runtime maps it onto a render grid with
the same numpy-only index arithmetic (no netCDF engine at runtime).
"""

from pathlib import Path

import numpy as np
import xarray as xr

SRC = str(Path(__file__).resolve().parents[1] / "src" / "app" / "assets" / "OCmask.nc")
OUT = "src/app/assets/ocean_mask.npz"


def main() -> None:
    with xr.open_dataset(SRC) as ds:
        m = ds["OCmask"].transpose("lat", "lon")
        lat = m["lat"].values.astype(np.float64)
        lon = m["lon"].values.astype(np.float64)
        valid = m.values == 1.0  # NaN == 1.0 is False → invalid

    # Orient north→south and lon ascending to match the render-grid convention
    # (resample_variables_to_grid uses linspace(lat_max, lat_min) / (lon_min, lon_max)).
    if lat[0] < lat[-1]:
        valid = valid[::-1]
        lat = lat[::-1]
    if lon[0] > lon[-1]:
        valid = valid[:, ::-1]
        lon = lon[::-1]

    res = float(abs(lat[1] - lat[0]))
    packed = np.packbits(valid)  # flatten C-order + pack to bits
    Path(OUT).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        OUT,
        packed=packed,
        shape=np.array(valid.shape, dtype=np.int64),
        res=np.float64(res),
        lon_min=np.float64(lon[0]),
        lat_max=np.float64(lat[0]),
    )
    print(
        f"wrote {OUT}: grid {valid.shape} ({valid.mean() * 100:.1f}% valid ocean), "
        f"lon [{lon[0]:.1f}, {lon[-1]:.1f}] lat [{lat[-1]:.1f}, {lat[0]:.1f}] "
        f"res {res:.3f}, packed {packed.nbytes / 1e3:.1f} KB"
    )


if __name__ == "__main__":
    main()
