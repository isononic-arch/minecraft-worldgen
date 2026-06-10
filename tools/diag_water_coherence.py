"""diag_water_coherence.py — S92: quantify checkered/floating river water +
audit river geometry (depth/width vs Strahler order, trench depth).

Reads a SURF_DUMP_STEP9_DIR dump (sy_final/rwy/rmeta9). Metrics:
  - incoherent cells: river cells whose rwy differs from the 3x3 river-median
    (the checkered-water population; MC renders +1 outliers as floating water,
    -1 outliers as holes)
  - floating cells: rwy strictly above ALL river 4-neighbours
  - water depth (rwy - bed) distribution, overall and by Strahler order
  - bank relief: local bank height above bed (trench-iness)
  - channel width by order (2 * interior EDT at skeleton cells)

Usage: py tools/diag_water_coherence.py <dump_dir> <tx> <tz>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.ndimage import distance_transform_edt, median_filter, maximum_filter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(d, tx, tz):
    sy = np.load(f"{d}/sy_final_{tx}_{tz}.npy").astype(np.int32)
    rwy = np.load(f"{d}/rwy_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{d}/rmeta9_{tx}_{tz}.npy")
    riv = ((rm == 1) | (rm == 2)) & (rwy > 63)
    print(f"({tx},{tz}) river cells: {riv.sum()}")
    if not riv.any():
        return

    # nearest-river fill -> 3x3 median -> compare (the coherence test)
    _, idx = distance_transform_edt(~riv, return_indices=True)
    fill = rwy[idx[0], idx[1]].astype(np.int16)
    med = median_filter(fill, size=3)
    incoh = riv & (med != rwy)
    print(f"  INCOHERENT (rwy != 3x3 river-median): {incoh.sum()} "
          f"({100.0*incoh.sum()/riv.sum():.1f}% of river)")

    # floating: above all river 4-neighbours
    big = np.full_like(rwy, 32767)
    rv = np.where(riv, rwy, big)
    n4 = np.minimum.reduce([np.roll(rv, 1, 0), np.roll(rv, -1, 0),
                            np.roll(rv, 1, 1), np.roll(rv, -1, 1)])
    floating = riv & (n4 < 32767) & (rwy > n4)
    print(f"  floating (above min river 4-nbr): {floating.sum()}")

    # depth audit
    depth = (rwy - sy)[riv]
    print(f"  water depth: p10={np.percentile(depth,10):.0f} p50={np.median(depth):.0f} "
          f"p90={np.percentile(depth,90):.0f} max={depth.max()}")

    # bank relief: highest land within 6px ring minus bed
    land = ~riv & (rwy < 0)
    sy_land = np.where(land, sy, -999)
    bank_max = maximum_filter(sy_land, size=13)  # 6px reach
    relief = (bank_max - sy)[riv & (bank_max > -999)]
    if relief.size:
        print(f"  bank-above-bed relief: p50={np.median(relief):.0f} "
              f"p90={np.percentile(relief,90):.0f} max={relief.max()}  (trench-iness)")

    # by Strahler order
    with rasterio.open(os.path.join(ROOT, "masks", "hydro_order.tif")) as ds:
        order = ds.read(1, window=Window(tx*512, tz*512, 512, 512)).astype(np.float32)
    # mask is either raw uint8 Strahler or normalized [0,1] (val/255)
    if order.max() <= 1.0:
        order = np.round(order * 255.0)
    order = order.astype(int)
    try:
        from skimage.morphology import skeletonize
        skel = skeletonize(riv)
    except Exception:
        skel = riv
    inside = distance_transform_edt(riv)
    for o in sorted(set(order[riv].tolist())):
        if o == 0:
            continue
        m = riv & (order == o)
        if m.sum() < 20:
            continue
        dep = (rwy - sy)[m]
        sk = skel & m
        wid = 2.0 * inside[sk] if sk.any() else np.array([0.0])
        print(f"  order {o}: cells={m.sum():6d}  depth p50={np.median(dep):.0f} "
              f"p90={np.percentile(dep,90):.0f} max={dep.max():2d}   "
              f"width p50={np.median(wid):.0f} p90={np.percentile(wid,90):.0f}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
