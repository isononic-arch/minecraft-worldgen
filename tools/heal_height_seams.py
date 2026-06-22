"""heal_height_seams.py — heal the systematic ~6-row vertical-overlap seam at
every z=512k (horizontal tile-row) boundary in masks/height.tif.

The raw mask was assembled from Gaea exports in 512-row horizontal strips with a
6-row vertical overlap placed edge-to-edge: each strip re-emits the previous
strip's last 6 rows, producing a backward elevation step at every tile-row
boundary (invisible on flats, up to ~21 MC-Y on steep highland). Vertical (x)
boundaries are clean and are NOT touched.

Heal = per-column cubic-Hermite bridge across the duplicate band, anchored on
clean rows just outside it, with clean local slopes. GEOMETRY-PRESERVING: no
rows are added/removed, so override.tif / hydro masks stay pixel-aligned. Only
the ~10 rows at each of the 96 boundaries change; everything else is byte-identical.

Default writes a NEW file (masks/height_healed.tif) — does NOT overwrite the
source. Pass --inplace to overwrite masks/height.tif (only after review).

Usage:
  py tools/heal_height_seams.py                  # -> masks/height_healed.tif
  py tools/heal_height_seams.py --dst masks/foo.tif
  py tools/heal_height_seams.py --inplace        # overwrite masks/height.tif
"""
import os, json, shutil, argparse
import numpy as np
import rasterio
from rasterio.windows import Window

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "masks", "height.tif")
CFG = os.path.join(ROOT, "config", "thresholds.json")
N = 50000
TILE = 512
MARG = 16          # read half-window around each boundary
A_OFF, B_OFF = -1, 10   # bridge anchors at R-1 (clean lower) and R+10 (clean upper, past 6-row dup)

_sp = json.load(open(CFG))["terrain_spline"]
_LUT = np.clip(np.interp(np.arange(65536.0), _sp["gaea_in"], _sp["mc_y_out"]),
               -60, 703).astype(np.int16)


def mcy(raw):
    return _LUT[np.clip(raw, 0, 65535).astype(np.int32)].astype(np.int32)


def heal_boundary(ds, R):
    """Read around boundary row R, Hermite-bridge the duplicate band per column,
    write back only the modified rows. Returns (before_n3, after_n3) at the seam."""
    R0 = R - MARG
    w = ds.read(1, window=Window(0, R0, N, 2 * MARG)).astype(np.float64)  # rows R0..R0+2M-1
    a, b = R + A_OFF, R + B_OFF
    ia, ib = a - R0, b - R0
    va, vb = w[ia].copy(), w[ib].copy()
    sa = (w[ia] - w[ia - 4]) / 4.0
    sb = (w[ib + 4] - w[ib]) / 4.0
    span = ib - ia
    before_n3 = int((np.abs(mcy(w[ia + 1]) - mcy(w[ia])) >= 3).sum())  # step at boundary row R
    patched = np.empty((span - 1, N), dtype=np.uint16)
    for j in range(1, span):
        t = j / span
        h00 = 2*t**3 - 3*t**2 + 1; h10 = t**3 - 2*t**2 + t
        h01 = -2*t**3 + 3*t**2;    h11 = t**3 - t**2
        row = h00*va + h10*span*sa + h01*vb + h11*span*sb
        patched[j - 1] = np.clip(np.round(row), 0, 65535).astype(np.uint16)
    # write back rows a+1 .. b-1  (== R .. R+9)
    ds.write(patched, 1, window=Window(0, a + 1, N, span - 1))
    # re-measure boundary step after
    after = ds.read(1, window=Window(0, R - 1, N, 2)).astype(np.int32)
    after_n3 = int((np.abs(mcy(after[1]) - mcy(after[0])) >= 3).sum())
    return before_n3, after_n3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dst", default=os.path.join(ROOT, "masks", "height_healed.tif"))
    ap.add_argument("--inplace", action="store_true",
                    help="overwrite masks/height.tif instead of writing a new file")
    args = ap.parse_args()
    dst = SRC if args.inplace else args.dst

    if not args.inplace:
        print(f"copying {SRC} -> {dst} (1.8 GB) ...", flush=True)
        shutil.copy2(SRC, dst)
    else:
        print("!! --inplace: healing masks/height.tif directly", flush=True)

    worst_before = worst_after = 0
    with rasterio.open(dst, "r+") as ds:
        for k in range(1, 97):
            R = TILE * k
            b3, a3 = heal_boundary(ds, R)
            worst_before = max(worst_before, b3)
            worst_after = max(worst_after, a3)
            if b3 > 0 or a3 > 0:
                print(f"  z={R:5d} (tile-bnd {k-1}|{k}): boundary n(step>=3) {b3:5d} -> {a3}",
                      flush=True)
    print(f"\nhealed 96 z=512k boundaries -> {dst}")
    print(f"  worst boundary n(step>=3):  before={worst_before}  after={worst_after}")


if __name__ == "__main__":
    main()
