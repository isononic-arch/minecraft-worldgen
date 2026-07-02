"""
tools/diag_river_raster_equiv.py — pixel-identity gate for the
_rasterize_river_edges_tile point-in-polygon replacement (S100 perf).

Context: cProfile of dense river tile (21,30) showed
matplotlib._path.points_in_path = 65.5s over 2 calls inside
core/hydro_region_overlay.py:_rasterize_river_edges_tile (the spline-
polygon inside-test that signs the SDF). This harness captures the FULL
output tuple of _rasterize_river_edges_tile on real data as reference
.npy files, then re-runs after the numpy scanline replacement and
asserts np.array_equal on every array. Timing for old/new is recorded.

Usage (run from repo root with the project python):
  py tools/diag_river_raster_equiv.py capture          # save refs (run BEFORE the edit)
  py tools/diag_river_raster_equiv.py verify           # run fast + slow paths, assert identity vs refs
  py tools/diag_river_raster_equiv.py verify --skip-slow   # only the (new) default path

Ref dir defaults to the session scratchpad; override with
--ref-dir or env VANDIR_RASTER_REF_DIR.

Memory: loads the production bed cache once (~1.5 GB peak, same as any
pipeline worker), then each tile is 512x512 work. No 50k allocations.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

_DEFAULT_REF_DIR = os.environ.get(
    "VANDIR_RASTER_REF_DIR",
    r"C:\Users\nicho\AppData\Local\Temp\claude"
    r"\C--Users-nicho-minecraft-worldgen"
    r"\f353d0d6-fe60-44d5-959c-f8dbcb30af33\scratchpad\raster_ref",
)

# Real painted-river tiles where the spline-polygon inside-test does work
# (>=1 polygon passes the bbox cull; verified against masks/_spline_cache.pkl):
#   (21,30) 2 polys / 60k verts  — the profiled dense tile
#   (22,30) 1 poly  / 55k verts
#   (62,61) 2 polys / 90k verts  — junction cascade reference tile
#   (19,76) 2 polys / 21k verts  — near-coast tributaries
#   (79,71) 1 poly  / 80k verts  — biggest single polygon in the cache
TILES = [(21, 30), (22, 30), (62, 61), (19, 76), (79, 71)]

TILE_SIZE = 512
# Names for the 6-tuple returned by _rasterize_river_edges_tile.
OUT_NAMES = ("out", "width", "paint_smooth_full_50k", "flow_50k",
             "carve_depth_50k", "sdf_blocks")


def _load_module():
    import core.hydro_region_overlay as hro
    hr_path = _REPO / "masks" / "hydro_region.png"
    if not hr_path.exists():
        sys.exit(f"FATAL: {hr_path} missing")
    t0 = time.perf_counter()
    hro._ensure_caches(hr_path)
    print(f"[harness] caches ready in {time.perf_counter() - t0:.1f}s")
    if not hro._river_spline_polygons_50k_cache:
        sys.exit("FATAL: spline polygon cache empty — inside-test would "
                 "never run; refs would be vacuous")
    return hro


def _run_tile(hro, tx: int, tz: int):
    col_off = tx * TILE_SIZE
    row_off = tz * TILE_SIZE
    t0 = time.perf_counter()
    result = hro._rasterize_river_edges_tile(col_off, row_off, TILE_SIZE)
    dt = time.perf_counter() - t0
    return result, dt


def _tile_dir(ref_dir: Path, tx: int, tz: int) -> Path:
    return ref_dir / f"tile_{tx}_{tz}"


def _save_ref(ref_dir: Path, tx: int, tz: int, result, dt: float) -> None:
    td = _tile_dir(ref_dir, tx, tz)
    td.mkdir(parents=True, exist_ok=True)
    meta = {"tile": [tx, tz], "elapsed_s": dt, "none": []}
    for name, arr in zip(OUT_NAMES, result):
        if arr is None:
            meta["none"].append(name)
            continue
        arr = np.asarray(arr)
        np.save(td / f"{name}.npy", arr)
        meta[name] = {"dtype": str(arr.dtype), "shape": list(arr.shape),
                      "nnz": int(np.count_nonzero(arr))}
    (td / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"[capture] tile ({tx},{tz}) saved ({dt:.1f}s)  "
          + "  ".join(f"{n}:nnz={meta[n]['nnz']}" for n in OUT_NAMES
                      if n in meta))


def _compare(ref_dir: Path, tx: int, tz: int, result, label: str) -> bool:
    td = _tile_dir(ref_dir, tx, tz)
    meta = json.loads((td / "meta.json").read_text())
    ok = True
    for name, arr in zip(OUT_NAMES, result):
        if name in meta["none"]:
            if arr is not None:
                print(f"  [{label}] {name}: FAIL ref=None, got array")
                ok = False
            continue
        if arr is None:
            print(f"  [{label}] {name}: FAIL ref=array, got None")
            ok = False
            continue
        ref = np.load(td / f"{name}.npy")
        arr = np.asarray(arr)
        if arr.dtype != ref.dtype:
            print(f"  [{label}] {name}: FAIL dtype {arr.dtype} != {ref.dtype}")
            ok = False
            continue
        if not np.array_equal(arr, ref):
            diff = arr != ref
            # array_equal is False for NaN==NaN; report real mismatches
            if arr.dtype.kind == "f":
                both_nan = np.isnan(arr) & np.isnan(ref)
                diff = diff & ~both_nan
            n = int(np.count_nonzero(diff))
            if n == 0:
                continue  # only NaN-position agreement — identical
            idx = np.argwhere(diff)[:5]
            print(f"  [{label}] {name}: FAIL {n} px differ, first at "
                  f"{idx.tolist()}")
            ok = False
    return ok


def cmd_capture(ref_dir: Path) -> None:
    hro = _load_module()
    timings = {}
    for tx, tz in TILES:
        result, dt = _run_tile(hro, tx, tz)
        _save_ref(ref_dir, tx, tz, result, dt)
        timings[f"{tx}_{tz}"] = dt
    (ref_dir / "timings_capture.json").write_text(json.dumps(timings, indent=1))
    print(f"[capture] DONE — refs in {ref_dir}")


def cmd_verify(ref_dir: Path, skip_slow: bool) -> None:
    hro = _load_module()
    all_ok = True
    rows = []
    for tx, tz in TILES:
        os.environ.pop("VANDIR_SLOW_RASTER", None)
        result_fast, dt_fast = _run_tile(hro, tx, tz)
        ok_fast = _compare(ref_dir, tx, tz, result_fast, "fast")
        dt_slow = None
        ok_slow = None
        if not skip_slow:
            os.environ["VANDIR_SLOW_RASTER"] = "1"
            try:
                result_slow, dt_slow = _run_tile(hro, tx, tz)
                ok_slow = _compare(ref_dir, tx, tz, result_slow, "slow")
            finally:
                os.environ.pop("VANDIR_SLOW_RASTER", None)
        all_ok &= ok_fast and (ok_slow is not False)
        rows.append((tx, tz, ok_fast, dt_fast, ok_slow, dt_slow))
    print()
    print(f"{'tile':>9} | {'fast':>5} {'t_fast':>8} | {'slow':>5} {'t_slow':>8} | speedup")
    for tx, tz, okf, dtf, oks, dts in rows:
        sp = f"{dts / dtf:6.1f}x" if dts else "     —"
        oks_s = {True: "PASS", False: "FAIL", None: "skip"}[oks]
        print(f"({tx:>3},{tz:>3}) | {'PASS' if okf else 'FAIL':>5} "
              f"{dtf:7.2f}s | {oks_s:>5} "
              f"{'' if dts is None else f'{dts:7.2f}s':>8} | {sp}")
    print()
    print("VERDICT:", "PIXEL-IDENTICAL on all tiles"
          if all_ok else "MISMATCH — see FAIL lines above")
    sys.exit(0 if all_ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["capture", "verify"])
    ap.add_argument("--ref-dir", default=_DEFAULT_REF_DIR)
    ap.add_argument("--skip-slow", action="store_true",
                    help="verify: skip re-running the matplotlib path")
    args = ap.parse_args()
    ref_dir = Path(args.ref_dir)
    if args.mode == "capture":
        ref_dir.mkdir(parents=True, exist_ok=True)
        cmd_capture(ref_dir)
    else:
        if not ref_dir.exists():
            sys.exit(f"FATAL: ref dir {ref_dir} missing — run capture first")
        cmd_verify(ref_dir, args.skip_slow)


if __name__ == "__main__":
    main()
