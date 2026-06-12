"""diag_width_map.py — S93e: headwater width-map calibration.

For each tile (from SURF_DUMP_STEP9_DIR dumps + the 8k caches), walks the
river skeleton and measures, per arc-length point:
  wet_hw   — wet half-width from the rendered dump (EDT inside the
             river mask at the skeleton cell)
  flow     — upstream-cell count sampled from _flow_accum_8k_cache
  dist_oc  — index along the path (proxy for distance downstream)

Prints percentile tables + a calibration preview for
  target_hw = clamp(K * flow**B, HW_MIN, None)
so K/B/HW_MIN can be chosen to (a) leave mainstem tiles untouched
(target >= wet_hw everywhere) and (b) pinch headwater tips to ~1.5-2.5.

Usage:
  py tools/diag_width_map.py <dump_dir> <tx,tz> [<tx,tz> ...] [--k 0.8 --b 0.45 --hwmin 1.5]
"""
import pickle
import sys

import numpy as np
from scipy.ndimage import distance_transform_edt, map_coordinates
from skimage.morphology import skeletonize


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    d = args[0]
    tiles = [tuple(map(int, t.split(","))) for t in args[1:] if "," in t]
    def opt(name, dv):
        return float(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else dv
    K, B, HWMIN = opt("--k", 0.8), opt("--b", 0.45), opt("--hwmin", 1.5)

    with open("masks/_bed_cache_v19.pkl", "rb") as f:
        flow8k = pickle.load(f)["flow_accum_8k"].astype(np.float32)
    S = 8192 / 50000.0

    for tx, tz in tiles:
        rwy = np.load(f"{d}/rwy_{tx}_{tz}.npy").astype(np.int32)
        rm = np.load(f"{d}/rmeta9_{tx}_{tz}.npy")
        riv = ((rm == 1) | (rm == 2)) & (rwy > 63)
        if not riv.any():
            print(f"({tx},{tz}) no river")
            continue
        wet_hw = distance_transform_edt(riv)
        skel = skeletonize(riv)
        sr, sc = np.where(skel)
        # flow at skeleton cells — the 8k accumulation lives on a 1px
        # skeleton, so point-bilinear misses it; window-MAX (5px grey
        # dilation) captures the nearest skeleton cell's value.
        from scipy.ndimage import grey_dilation
        rows = (sr + tz * 512) * S
        cols = (sc + tx * 512) * S
        r0 = max(0, int(rows.min()) - 8); r1 = min(8192, int(rows.max()) + 9)
        c0 = max(0, int(cols.min()) - 8); c1 = min(8192, int(cols.max()) + 9)
        sub = grey_dilation(flow8k[r0:r1, c0:c1], size=5)
        fl = map_coordinates(sub, np.stack([rows - r0, cols - c0]), order=1,
                             mode="constant", cval=0.0)
        hw = wet_hw[sr, sc]
        tgt = np.maximum(K * np.power(np.maximum(fl, 1.0), B), HWMIN)
        pinched = tgt < hw
        print(f"({tx},{tz}) skel cells {len(sr)}")
        for name, v in (("wet_hw", hw), ("flow", fl), ("target_hw", tgt)):
            q = np.percentile(v, [5, 25, 50, 75, 95])
            print(f"  {name:9s} p5/25/50/75/95: "
                  + " ".join(f"{x:8.1f}" for x in q))
        print(f"  cells pinched (target<wet): {int(pinched.sum())} "
              f"({100.0 * pinched.mean():.0f}%)  "
              f"pinch ratio p50: "
              f"{np.percentile((tgt / np.maximum(hw, 0.1))[pinched], 50) if pinched.any() else float('nan'):.2f}")
        # tip vs mouth thirds (by flow ordering)
        order = np.argsort(fl)
        third = max(1, len(order) // 3)
        for nm, idx in (("low-flow third (tips)", order[:third]),
                        ("high-flow third (mainstem)", order[-third:])):
            print(f"  {nm}: wet_hw p50 {np.percentile(hw[idx], 50):.1f}  "
                  f"flow p50 {np.percentile(fl[idx], 50):.0f}  "
                  f"target p50 {np.percentile(tgt[idx], 50):.1f}")


if __name__ == "__main__":
    main()
