"""rebuild_river_wl.py — S94 GLOBAL river water-level bake (the seam-clean fix).

Computes the river water surface ONCE at full resolution over the whole map, so
every tile reads an identical, seam-continuous level. ONLY the LEVEL is global;
the river EXTENT stays per-tile (chunk_writer fills water where level > bed at
full res), so the shoreline/perimeter remains organic — no NEAREST-upscale
"swimming pool" geometry.

Reuses core.river_flood_settle.settle() exactly as the per-tile pass does, but
with a GLOBAL dist-from-real-ocean (one field for the whole map) instead of the
per-tile dist-from-local-ocean whose pseudo-ocean seed differed between adjacent
tiles and produced the 1-block water-level seam step.

Inputs (50k masks): height, hydro_centerline, hydro_width, hydro_lake.
Output: masks/hydro_river_wl.tif (int16; water-surface MC-Y on river cells +
a propagated coverage band, -999 elsewhere).

MEMORY: full 50k arrays + an EDT with return_indices => tens of GB peak. Run on
a 192GB box, NOT the 7.5GB local machine. --scale N downsamples for local smoke
tests (N=8 reproduces the known-too-low 1:8 result; only N=1 is production).

Usage:  python rebuild_river_wl.py [--scale 1] [--masks masks] [--out masks/hydro_river_wl.tif]
"""
import argparse
import sys
import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy import ndimage

sys.path.insert(0, ".")
from core import column_generator as _cg
from core import river_flood_settle as _fs

LUT = _cg._LUT
SEA = _cg.SEA_LEVEL
FULL = 50000


def _read_full(path):
    with rasterio.open(path) as s:
        return s.read(1)


def _read_scaled(path, n, resamp):
    with rasterio.open(path) as s:
        return s.read(1, out_shape=(n, n), resampling=resamp)


def _block_max(path, n, scale):
    """Block-MAX downsample to n×n (preserves thin centerline/width)."""
    out = np.zeros((n, n), dtype=np.float32)
    CH = max(1, 800 // scale)
    with rasterio.open(path) as s:
        W = s.width
        WT = (W // scale) * scale
        for o0 in range(0, n, CH):
            o1 = min(o0 + CH, n)
            blk = s.read(1, window=((o0*scale, o1*scale), (0, WT))).astype(np.float32)
            nr = (o1 - o0)
            blk = blk[:nr*scale, :WT].reshape(nr, scale, WT // scale, scale)
            out[o0:o1, :WT // scale] = blk.max(axis=(1, 3))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=1, help="1 = full 50k (production)")
    ap.add_argument("--masks", default="masks")
    ap.add_argument("--out", default="masks/hydro_river_wl.tif")
    ap.add_argument("--cover", type=int, default=24,
                    help="blocks to propagate level beyond the channel (coverage)")
    a = ap.parse_args()
    sc = a.scale
    N = FULL // sc
    print(f"[river_wl] scale={sc} grid={N}x{N}", flush=True)

    M = a.masks
    if sc == 1:
        h = _read_full(f"{M}/height.tif").astype(np.uint16)
        cl = (_read_full(f"{M}/hydro_centerline.tif") > 0)
        wid = _read_full(f"{M}/hydro_width.tif").astype(np.float32)
        lake = (_read_full(f"{M}/hydro_lake.tif") > 0)
    else:
        h = _read_scaled(f"{M}/height.tif", N, Resampling.bilinear).astype(np.uint16)
        cl = _block_max(f"{M}/hydro_centerline.tif", N, sc) > 0
        wid = _block_max(f"{M}/hydro_width.tif", N, sc)
        lake = _block_max(f"{M}/hydro_lake.tif", N, sc) > 0

    bed = LUT[h].astype(np.int32)
    del h
    print(f"[river_wl] bed built; centerline cells={int(cl.sum())}", flush=True)

    # --- channel footprint from centerline + width (carver tube logic) -------
    # dist-to-centerline + width-of-nearest-centerline -> river where dcl <= w/2.
    dcl, (iy, ix) = ndimage.distance_transform_edt(~cl, return_indices=True)
    w_at_cl = (wid[iy, ix] / float(sc))           # width in this grid's px
    del wid, iy, ix
    river = (dcl <= np.maximum(w_at_cl / 2.0, 0.5)) & ~lake
    del dcl, w_at_cl
    print(f"[river_wl] river footprint cells={int(river.sum())}", flush=True)

    source = np.where(river, bed - 1, -999).astype(np.int32)
    ocean = bed <= SEA
    # GLOBAL dist-from-real-ocean (the seam fix: one field, no per-tile seed)
    dist = ndimage.distance_transform_edt(~ocean).astype(np.float32)
    del ocean
    land = ~river & ~lake
    print("[river_wl] running global settle()...", flush=True)
    level = _fs.settle(source=source, bed=bed, river=river,
                       dist=dist, skel=cl, land=land)
    del source, dist, land, cl
    print(f"[river_wl] settle done; water cells={int((level > SEA).sum())}", flush=True)

    # --- propagate level beyond the channel so per-tile carver river cells
    #     (which include bank dilation) all find a level (nearest-fill, capped) -
    have = level > SEA
    coverpx = max(1, a.cover // sc)
    dfill, (jy, jx) = ndimage.distance_transform_edt(~have, return_indices=True)
    out = np.where(dfill <= coverpx, level[jy, jx], -999).astype(np.int16)
    print(f"[river_wl] covered cells={int((out > SEA).sum())} "
          f"(cover band {coverpx}px)", flush=True)

    # --- write (upscale to 50k if scaled) ------------------------------------
    if sc != 1:
        big = np.repeat(np.repeat(out, sc, 0), sc, 1)[:FULL, :FULL]
        out = big
    with rasterio.open(f"{M}/hydro_centerline.tif") as ref:
        prof = ref.profile
    prof.update(dtype="int16", count=1, compress="deflate", nodata=-999)
    with rasterio.open(a.out, "w", **prof) as dst:
        dst.write(out.astype(np.int16), 1)
    print(f"[river_wl] WROTE {a.out}  shape={out.shape} "
          f"water-cells={int((out > SEA).sum())}", flush=True)


if __name__ == "__main__":
    main()
