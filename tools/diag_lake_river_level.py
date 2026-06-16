"""diag_lake_river_level.py — RENDERED lake-water diagnostics from the MCA.

Reports, for a tile:
  - LAKE-PERCH: lake water cells whose 4-neighbour DRY land top sits BELOW the
    lake surface (= lake water sitting above its own shore — the clearest
    "lake sits too high" signal).
  - JUNCTION: lake water-top vs nearby river water-top, measured at a 12-block
    reach (past the cascade-blend start) so a real lake>river STEP shows up.

Usage: py tools/diag_lake_river_level.py <mca_dir> <tx> <tz>
"""
import sys
import numpy as np
import rasterio
from scipy import ndimage
sys.path.insert(0, "tools")
from diag_mca_surface_perch import tile_surface   # wt (water-top), ct (col-top), Y 50-320


def main(mdir, tx, tz):
    mca = f"{mdir}/r.{tx}.{tz}.mca"
    wt, ct = tile_surface(mca)
    z0, x0 = tz * 512, tx * 512
    with rasterio.open("masks/hydro_lake.tif") as s:
        lake = s.read(1, window=((z0, z0 + 512), (x0, x0 + 512))) > 0
    with rasterio.open("masks/hydro_centerline.tif") as s:
        cl = s.read(1, window=((z0, z0 + 512), (x0, x0 + 512))) > 0
    river = cl & ~lake
    water = wt > -999
    dry = (~water) & (ct > -999)
    lakeW = lake & water
    riverW = river & water
    INF = 1 << 20

    # --- LAKE-PERCH: lake water above an adjacent DRY land top ---
    dryv = np.where(dry, ct, INF)
    min_dry = np.full(ct.shape, INF, np.int32)
    for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        min_dry = np.minimum(min_dry, np.roll(np.roll(dryv, dz, 0), dx, 1))
    lake_perch = lakeW & (min_dry < INF) & (wt > min_dry + 1)
    npr = int(lake_perch.sum())

    # --- JUNCTION level: lake-top vs river-top at 12-block reach ---
    rtop = np.where(riverW, wt, 0).astype(np.float32)
    for _ in range(12):
        rtop = ndimage.grey_dilation(rtop, size=3)
    edge = lakeW & ndimage.binary_dilation(riverW, iterations=12) & (rtop > 0)
    n = int(edge.sum())

    print(f"=== ({tx},{tz}) lakeWater={int(lakeW.sum())} riverWater={int(riverW.sum())} ===")
    print(f"   LAKE-PERCH (lake water > adjacent dry shore): {npr} "
          f"({100.0*npr/max(1,int(lakeW.sum())):.2f}% of lake water)")
    if npr:
        zz, xx = np.where(lake_perch); over = (wt - min_dry)[zz, xx]
        for i in np.argsort(-over)[:6]:
            z, x = int(zz[i]), int(xx[i])
            print(f"     world({x0+x},{z0+z}) lake={int(wt[z,x])} shore={int(min_dry[z,x])} "
                  f"+{int(over[i])}")
    if n:
        d = (wt[edge].astype(np.int32) - rtop[edge].astype(np.int32))
        hi = int((d > 1).sum())
        print(f"   JUNCTION(reach12) cells={n}  lake-river: >+1(lake higher)={hi} "
              f"({100.0*hi/n:.0f}%)  max=+{int(d.max())} mean={d.mean():+.2f} "
              f"median={int(np.median(d))}")
    else:
        print("   JUNCTION: no lake/river contact within 12 blocks")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
