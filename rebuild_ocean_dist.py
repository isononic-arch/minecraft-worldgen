"""rebuild_ocean_dist.py — GLOBAL distance-from-ocean field (the seam-clean fix
for the flood-settle monotone).

The flood-settle orders cross-sections by distance-from-ocean and walks a
running-min downstream. run_pipeline computes that distance with a PER-TILE EDT
on the padded window; when the ocean is beyond the 48px halo it falls back to the
lowest in-window cell, so the distance — and therefore the band ordering and the
final water level — DIFFERS by ~1 across a tile seam on flowing rivers (52,53:
387 ±1 water-step columns). A single GLOBAL distance field read identically on
both sides removes that divergence.

Computed at 1:8 (EDT is cheap there; the full-50k EDT OOMs and materializing a
50k float32 is 10GB > local RAM). run_pipeline reads a window and upscales it to
the padded tile — distance is smooth so bilinear is exact enough for ORDERING
(the settle only uses the relative order, not absolute metres).

Output: masks/hydro_ocean_dist8.tif (6250x6250 float32, distance in 1:8 px).

Usage: python rebuild_ocean_dist.py [--masks masks]
"""
import argparse
import sys
import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy import ndimage

sys.path.insert(0, ".")
from core import column_generator as _cg

LUT = _cg._LUT
SEA = _cg.SEA_LEVEL
FULL = 50000
SC = 8
N = FULL // SC


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--masks", default="masks")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = a.out or f"{a.masks}/hydro_ocean_dist8.tif"

    with rasterio.open(f"{a.masks}/height.tif") as s:
        h = s.read(1, out_shape=(N, N), resampling=Resampling.bilinear).astype(np.uint16)
        prof = s.profile
    bed = LUT[h].astype(np.int32)
    ocean = bed <= SEA
    print(f"[ocean_dist] ocean px @1:8 = {int(ocean.sum())}/{N*N}", flush=True)
    if not ocean.any():
        print("[ocean_dist] NO ocean — using lowest cell as sink", flush=True)
        ocean = np.zeros((N, N), bool)
        ocean.ravel()[int(np.argmin(bed))] = True
    # distance (in 1:8 px) from every cell to the nearest ocean cell
    dist = ndimage.distance_transform_edt(~ocean).astype(np.float32)
    print(f"[ocean_dist] dist range {float(dist.min()):.0f}..{float(dist.max()):.0f} "
          f"(1:8 px)", flush=True)

    prof.update(dtype="float32", count=1, compress="deflate", nodata=None,
                width=N, height=N,
                transform=rasterio.Affine(SC, 0, 0, 0, SC, 0))
    with rasterio.open(out, "w", **prof) as dst:
        dst.write(dist, 1)
    print(f"[ocean_dist] WROTE {out} shape={dist.shape}", flush=True)


if __name__ == "__main__":
    main()
