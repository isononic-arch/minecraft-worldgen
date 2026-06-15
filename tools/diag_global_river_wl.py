"""Offline validation of the GLOBAL river water-level approach BEFORE wiring it
into the pipeline. Computes the level once on the 1:8 global arrays via the same
settle() used per-tile (but with a GLOBAL dist-from-real-ocean), then compares
it cell-by-cell against the per-tile flood-settle water already captured in the
seam-pair dump (the height the user approved). If global ~= per-tile, the global
approach reproduces the approved water AND is seam-clean by construction.

Usage: py tools/diag_global_river_wl.py <dump_dir> <txA> <tzA> <txB> <tzB>
"""
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

SCALE = 8
N8 = 6250


def r8(name, rs=Resampling.nearest):
    with rasterio.open(f"masks/{name}") as s:
        return s.read(1, out_shape=(N8, N8), resampling=rs)


def r8_max(name):
    """Block-MAX downsample 50k -> 1:8 (preserves thin features). Chunked read
    so the full 50k array never lands in memory at once."""
    out = np.zeros((N8, N8), dtype=np.float32)
    CH = 100  # output rows per chunk = 800 full-res rows
    with rasterio.open(f"masks/{name}") as s:
        W = s.width
        WT = (W // SCALE) * SCALE
        for o0 in range(0, N8, CH):
            o1 = min(o0 + CH, N8)
            r0 = o0 * SCALE; r1 = o1 * SCALE
            blk = s.read(1, window=((r0, r1), (0, WT))).astype(np.float32)
            nr = (r1 - r0) // SCALE
            blk = blk[:nr*SCALE, :WT].reshape(nr, SCALE, WT // SCALE, SCALE)
            out[o0:o0+nr, :WT // SCALE] = blk.max(axis=(1, 3))
    return out


def main(dump, txA, tzA, txB, tzB):
    print("loading 1:8 masks...")
    h8 = r8("height.tif", Resampling.bilinear).astype(np.uint16)
    cl8 = r8_max("hydro_centerline.tif")        # block-max keeps thin centerline
    wid8 = r8_max("hydro_width.tif")
    lake8 = r8_max("hydro_lake.tif")
    bed8 = LUT[h8].astype(np.int32)
    skel8 = cl8 > 0
    # river footprint at 1:8: dilate centerline by ~ (width/8) so wide rivers are
    # captured (a 60-block river ~ 7px at 1:8).
    river8 = skel8.copy()
    wpx = np.clip((wid8 / SCALE).astype(int), 0, 8)
    for it in range(1, 9):
        river8 |= ndimage.binary_dilation(skel8 & (wpx >= it),
                                          iterations=it)
    lake8m = lake8 > 0
    river8 &= ~lake8m
    source8 = np.where(river8, bed8 - 1, -999).astype(np.int32)
    ocean8 = bed8 <= SEA
    print(f"river8 cells={int(river8.sum())} ocean8 cells={int(ocean8.sum())}")
    dist8 = ndimage.distance_transform_edt(~ocean8).astype(np.float32)
    land8 = ~river8 & ~lake8m
    print("running global settle()...")
    level8 = _fs.settle(source=source8, bed=bed8, river=river8,
                        dist=dist8, skel=skel8, land=land8)

    # propagate level to a band around the river so 50k footprint cells covered
    have = level8 > SEA
    _, (iy, ix) = ndimage.distance_transform_edt(~have, return_indices=True)
    dband = ndimage.distance_transform_edt(~have)
    levelfill = np.where(dband <= 8, level8[iy, ix], -999)

    # --- compare to per-tile approved water at both seam tiles ---
    for (tx, tz) in ((txA, tzA), (txB, tzB)):
        rwy = np.load(f"{dump}/rwy_{tx}_{tz}.npy").astype(np.int32)   # inner 512
        rm = np.load(f"{dump}/rmeta9_{tx}_{tz}.npy")
        riv = ((rm == 1) | (rm == 2)) & (rwy > SEA)
        # tile world origin -> 1:8 index
        zz, xx = np.where(riv)
        wz = tz * 512 + zz; wx = tx * 512 + xx
        gi = np.clip(wz // SCALE, 0, N8 - 1); gj = np.clip(wx // SCALE, 0, N8 - 1)
        glob = levelfill[gi, gj]
        ok = glob > SEA
        d = glob[ok] - rwy[zz, xx][ok]
        cov = 100.0 * ok.mean() if ok.size else 0
        print(f"\n({tx},{tz}): river-water cells={riv.sum()}  global-covered={cov:.1f}%")
        if ok.any():
            print(f"  (global_level - approved_water): mean={d.mean():+.2f} "
                  f"median={np.median(d):+.0f} std={d.std():.2f} "
                  f"min={d.min()} max={d.max()}")
            import collections
            c = collections.Counter(d.tolist())
            print("  delta histogram:", dict(sorted(c.items())))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]),
         int(sys.argv[4]), int(sys.argv[5]))
