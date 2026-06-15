"""Mask-based perch/levee scan: using ONLY the global hydro_river_wl level + the
terrain bed, find cells where the water would sit ABOVE adjacent dry land (the
overspill the user reported). wet = level>bed; perch = a wet cell whose water
level exceeds the bed of an adjacent cell that is NOT wet (so the water there is
unsupported -> spills/perches). No per-tile dump needed.

Usage: py tools/diag_mask_perch.py <tx> <tz> [tx tz ...]
"""
import sys
import numpy as np
import rasterio
from rasterio.windows import Window
sys.path.insert(0, ".")
from core import column_generator as _cg
LUT = _cg._LUT
SEA = _cg.SEA_LEVEL


def scan(tx, tz):
    x0, z0 = tx * 512, tz * 512
    with rasterio.open("masks/hydro_river_wl.tif") as s:
        lvl = s.read(1, window=Window(x0, z0, 512, 512)).astype(np.int32)
    with rasterio.open("masks/height.tif") as s:
        h = s.read(1, window=Window(x0, z0, 512, 512))
    bed = LUT[h].astype(np.int32)
    have = lvl > SEA
    wet = have & (lvl > bed)                 # cells the global level actually floods
    # adjacent NON-wet surface (the land the water could spill onto)
    INF = 1 << 20
    nonwet_bed = np.where(~wet, bed, INF)
    min_adj_dry = np.full(bed.shape, INF, np.int32)
    for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        min_adj_dry = np.minimum(min_adj_dry, np.roll(np.roll(nonwet_bed, dz, 0), dx, 1))
    # perch: wet cell whose water level is ABOVE an adjacent dry land top
    perch = wet & (min_adj_dry < INF) & (lvl > min_adj_dry + 1)   # >1 = visible spill
    n_wet = int(wet.sum()); n_perch = int(perch.sum())
    print(f"=== ({tx},{tz}) covered={int(have.sum())} wet(level>bed)={n_wet} "
          f"PERCH(level>adj dry land+1)={n_perch} ({100.0*n_perch/max(1,n_wet):.1f}% of wet) ===")
    if n_perch:
        zz, xx = np.where(perch)
        over = (lvl - min_adj_dry)[zz, xx]
        o = np.argsort(-over)[:10]
        for i in o:
            z, x = zz[i], xx[i]
            print(f"   ({z},{x}) world({x0+x},{z0+z}) level={lvl[z,x]} bed={bed[z,x]} "
                  f"adj_dry_land={min_adj_dry[z,x]} -> water +{over[i]} above dry land")


def main(args):
    pairs = [(int(args[i]), int(args[i+1])) for i in range(0, len(args), 2)]
    for tx, tz in pairs:
        scan(tx, tz)


if __name__ == "__main__":
    main(sys.argv[1:])
