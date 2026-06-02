"""Concurrency-safety contract for the numba render kernels.

The resample/normalize kernels are ``@njit(parallel=True)``. Numba runs a
parallel region through its *threading layer*; when neither TBB nor OpenMP is
installed (a stock pip install), it falls back to ``workqueue``, which is **not
threadsafe** — entering a parallel region from two Python threads at once
corrupts the layer's shared scheduler state. Our sync tile handlers run in the
AnyIO thread pool, so a burst of tiles enters those regions concurrently: the
output PNG comes back with a garbled mask channel (full-range alpha instead of a
0/255 ocean mask), or, on a numba build with the concurrency guard, the
interpreter aborts with SIGABRT.

``resample_variables_to_grid`` and ``normalize`` serialise *entry* into the
parallel kernels with a process-wide lock, so concurrent callers stay
byte-for-byte identical to the single-threaded result. This test drives both
functions from many threads and asserts that invariant.

The workload runs in a **subprocess**: on a ``workqueue`` build the unguarded
code aborts the interpreter, which would take down the whole pytest session
rather than failing one test. The subprocess isolates that into a clean
non-zero exit we can assert on.
"""

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"

# Hammer resample_variables_to_grid + normalize from many threads and assert every
# concurrent result matches the single-threaded golden output bit-for-bit, and that
# the per-pixel valid mask stays strictly binary (the channel that gets garbled
# under the workqueue race).
_WORKLOAD = """
import numpy as np
import xarray as xr
from concurrent.futures import ThreadPoolExecutor

from app.services.rendering.kernels import (
    normalize,
    resample_variables_to_grid,
    warmup_resample,
)

warmup_resample()  # prime the JIT single-threaded, exactly like server startup

rng = np.random.default_rng(0)
lat = np.linspace(-10.0, -40.0, 128)  # north -> south
lon = np.linspace(110.0, 155.0, 128)
data = rng.random((128, 128)) * 30.0
data[rng.random((128, 128)) < 0.1] = np.nan  # NaN holes exercise the valid-mask path
ds = xr.Dataset({"sst": xr.DataArray(data, dims=["lat", "lon"], coords={"lat": lat, "lon": lon})})


def compute():
    (r,) = resample_variables_to_grid(ds, ["sst"], 512, 512)
    n, valid = normalize(r, 0.0, 30.0, 16777215)
    return r, n, valid


gold_r, gold_n, gold_valid = compute()
assert set(np.unique(gold_valid)).issubset({0, 1}), "single-threaded mask is not binary"


def work(_):
    r, n, valid = compute()
    return (
        np.array_equal(np.nan_to_num(r), np.nan_to_num(gold_r))
        and np.array_equal(n, gold_n)
        and np.array_equal(valid, gold_valid)
        and set(np.unique(valid)).issubset({0, 1})
    )


with ThreadPoolExecutor(max_workers=32) as ex:
    results = list(ex.map(work, range(400)))

bad = results.count(False)
assert bad == 0, f"{bad}/400 concurrent kernel results were corrupted"
print("OK")
"""


def test_parallel_kernels_are_concurrency_safe():
    proc = subprocess.run(
        [sys.executable, "-c", _WORKLOAD],
        cwd=_ROOT,
        env={**os.environ, "PYTHONPATH": str(_SRC)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"concurrent kernel workload failed (exit {proc.returncode}). "
        "The parallel numba kernels are racing under concurrent calls.\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "OK" in proc.stdout
