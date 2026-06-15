"""Diagnose water perching/levees introduced by the GLOBAL river water-level
bake: compare the global level vs the approved per-tile flood-settle level vs the
local terrain + the local adjacent-land bank, on a tile's river cells. Perching =
water level higher than the lowest adjacent LAND top (water sits above terrain).

Usage: py tools/diag_water_perch.py <dump_dir> <tx> <tz>
  dump_dir holds rwy_{tx}_{tz}.npy / rmeta9_{tx}_{tz}.npy (per-tile approved water)
"""
import sys
import numpy as np
import rasterio
from rasterio.windows import Window
sys.path.insert(0, ".")
from core import column_generator as _cg
LUT = _cg._LUT
SEA = _cg.SEA_LEVEL


def main(dump, tx, tz):
    x0, z0 = tx * 512, tz * 512
    # per-tile approved water (the contained, user-approved level) + river meta
    rwy = np.load(f"{dump}/rwy_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{dump}/rmeta9_{tx}_{tz}.npy")
    river = ((rm == 1) | (rm == 2))
    # global level (window read of the baked mask)
    with rasterio.open("masks/hydro_river_wl.tif") as s:
        glob = s.read(1, window=Window(x0, z0, 512, 512)).astype(np.int32)
    # CARVED surface (the actual rendered ground) — NOT pre-carve height. The
    # river sits in a carved channel below pre-carve terrain, so the containing
    # BANK is the carved sy_final of adjacent land, not LUT[height].
    bed = np.load(f"{dump}/sy_final_{tx}_{tz}.npy").astype(np.int32)

    # min adjacent LAND top per cell (land = not river). This is the bank that
    # must contain the water (water above it perches/spills).
    land = ~river
    INF = 1 << 20
    bed_land = np.where(land, bed, INF)
    min_adj_land = np.full(bed.shape, INF, np.int32)
    for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        min_adj_land = np.minimum(min_adj_land, np.roll(np.roll(bed_land, dz, 0), dx, 1))

    rivwet = river & (rwy > SEA)
    have_g = rivwet & (glob > SEA)
    print(f"=== tile ({tx},{tz}) river-water cells={int(rivwet.sum())} "
          f"global-covered={int(have_g.sum())} ({100.0*have_g.sum()/max(1,rivwet.sum()):.0f}%) ===\n")

    # perching: level > lowest adjacent land top (water sits proud of terrain)
    valid_bank = min_adj_land < INF
    perch_pt = rivwet & valid_bank & (rwy > min_adj_land)
    perch_g = have_g & valid_bank & (glob > min_adj_land)
    print(f"PERCH (level > lowest adjacent land top):")
    print(f"  per-tile (approved): {int(perch_pt.sum())} cells")
    print(f"  global    (new bake): {int(perch_g.sum())} cells")

    # where global RAISED the level vs per-tile
    raised = have_g & (glob > rwy)
    print(f"\nGLOBAL vs PER-TILE on covered cells:")
    if have_g.any():
        d = (glob - rwy)[have_g]
        import collections
        print(f"  (global - per-tile): mean={d.mean():+.2f} median={int(np.median(d))} "
              f"min={int(d.min())} max={int(d.max())}")
        print(f"  raised (global>per-tile): {int(raised.sum())} cells")
        print(f"  delta hist: {dict(sorted(collections.Counter(d.tolist()).items()))}")

    # the worst perch cells under global: how high above the bank?
    if perch_g.any():
        zz, xx = np.where(perch_g)
        over = (glob - min_adj_land)[zz, xx]
        o = np.argsort(-over)[:12]
        print(f"\nWORST global-perch cells (water above lowest adjacent land):")
        for i in o:
            z, x = zz[i], xx[i]
            print(f"  ({z},{x}) glob={glob[z,x]} per-tile={rwy[z,x]} "
                  f"bed={bed[z,x]} min_adj_land={min_adj_land[z,x]} "
                  f"-> +{glob[z,x]-min_adj_land[z,x]} above bank")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
