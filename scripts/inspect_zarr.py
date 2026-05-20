#!/usr/bin/env python3
"""Inspect a Zarr store: variables, chunks, coordinate ranges, and time regularity.

Usage:
    uv run scripts/inspect_zarr.py s3://bucket/path/to/store.zarr
    uv run scripts/inspect_zarr.py /local/path/to/store.zarr
    S3_ANON=false uv run scripts/inspect_zarr.py s3://private-bucket/store.zarr
"""

import os
import sys

import numpy as np
import xarray as xr


def _storage_options(url: str) -> dict:
    if url.startswith("s3://"):
        anon = os.environ.get("S3_ANON", "true").lower() not in ("false", "0", "no")
        return {"anon": anon}
    return {}


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _check_time_regularity(times) -> tuple[bool, str]:
    if len(times) < 2:
        return True, "only one timestep"
    deltas = np.diff(times.astype("int64"))
    unique = np.unique(deltas)
    if len(unique) == 1:
        delta_ns = int(unique[0])
        delta_s = delta_ns / 1e9
        if delta_s % 86400 == 0:
            label = f"{int(delta_s // 86400)}d"
        elif delta_s % 3600 == 0:
            label = f"{int(delta_s // 3600)}h"
        else:
            label = f"{delta_s:.0f}s"
        return True, f"regular — every {label}"
    gap_days = deltas / 1e9 / 86400
    median = np.median(gap_days)
    # Find the first gap that deviates most from the median as a concrete example.
    worst_idx = int(np.argmax(np.abs(gap_days - median)))
    example = (
        f"e.g. {str(times[worst_idx])[:10]} → {str(times[worst_idx + 1])[:10]} "
        f"= {gap_days[worst_idx]:.1f}d"
    )
    return False, (
        f"irregular — min {gap_days.min():.1f}d, max {gap_days.max():.1f}d, "
        f"median {median:.1f}d  ({example})"
    )


def inspect(store_url: str) -> None:
    print(f"\nStore: {store_url}\n")

    opts = _storage_options(store_url)
    ds = xr.open_zarr(store_url, storage_options=opts if opts else None)

    # --- Coordinates ---
    print("Coordinates")
    print("-----------")
    for name, coord in ds.coords.items():
        shape = dict(zip(coord.dims, coord.shape, strict=False))
        print(f"  {name}: {coord.dtype}  shape={shape}")

    # --- Time regularity ---
    time_coord = next((ds.coords[c] for c in ("time", "TIME") if c in ds.coords), None)
    if time_coord is not None:
        times = np.sort(time_coord.values)
        regular, description = _check_time_regularity(times)
        flag = "✓" if regular else "✗"
        print(f"\nTime ({len(times)} steps): {flag} {description}")
        if len(times) >= 1:
            print(f"  first : {str(times[0])[:19]}")
            print(f"  last  : {str(times[-1])[:19]}")

    # --- Variables and chunks ---
    print("\nVariables and chunking")
    print("----------------------")
    data_vars = {k: v for k, v in ds.data_vars.items()}
    if not data_vars:
        print("  (none)")

    for name, var in data_vars.items():
        chunks = var.encoding.get("chunks") or var.encoding.get("preferred_chunks")
        dims_shape = dict(zip(var.dims, var.shape, strict=False))
        chunk_str = str(chunks) if chunks else "not chunked"

        # Estimate chunk size in bytes if we have chunk info
        if chunks:
            elem_bytes = var.dtype.itemsize
            chunk_elems = int(np.prod(chunks))
            chunk_size = _fmt_bytes(chunk_elems * elem_bytes)
            chunk_str = f"{chunks}  (~{chunk_size}/chunk)"

        print(f"  {name}: {var.dtype}")
        print(f"    dims   : {dims_shape}")
        print(f"    chunks : {chunk_str}")
        if var.attrs.get("units"):
            print(f"    units  : {var.attrs['units']}")
        if var.attrs.get("long_name"):
            print(f"    desc   : {var.attrs['long_name']}")

    # --- Spatial extent ---
    lat = ds.coords.get("lat") if "lat" in ds.coords else ds.coords.get("LATITUDE")
    lon = ds.coords.get("lon") if "lon" in ds.coords else ds.coords.get("LONGITUDE")
    if lat is not None and lon is not None:
        print("\nSpatial extent")
        print("--------------")
        print(f"  lat : {float(lat.min()):.4f} → {float(lat.max()):.4f}  ({lat.size} points)")
        print(f"  lon : {float(lon.min()):.4f} → {float(lon.max()):.4f}  ({lon.size} points)")

    print()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <store_url>")
        sys.exit(1)
    inspect(sys.argv[1])
