"""diag_water_levels.py — S93b: top-down river water-level visual gate.

Three panels from a SURF_DUMP_STEP9_DIR dump, cropped to the river bbox:
  1. water_y as discrete color bands — the staircase structure. GOOD =
     clean cross-channel bands stepping monotonically downstream. BAD =
     interleaved/checkered colors (the S92 floating-water artifact).
  2. water depth (rwy - sy_final) — holes (<=0) and trenches pop out.
  3. incoherent cells (rwy != 3x3 river-median) in red over the bands.

Usage:
  py tools/diag_water_levels.py <dump_dir> <tx> <tz> <out.png>
"""
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.ndimage import distance_transform_edt, median_filter


def main():
    d, tx, tz, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    sy = np.load(f"{d}/sy_final_{tx}_{tz}.npy").astype(np.int32)
    rwy = np.load(f"{d}/rwy_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{d}/rmeta9_{tx}_{tz}.npy")
    riv = ((rm == 1) | (rm == 2)) & (rwy > 0)
    if not riv.any():
        print(f"({tx},{tz}) no river cells")
        return
    rr, cc = np.where(riv)
    r0, r1 = max(rr.min() - 12, 0), min(rr.max() + 12, sy.shape[0])
    c0, c1 = max(cc.min() - 12, 0), min(cc.max() + 12, sy.shape[1])
    sl = np.s_[r0:r1, c0:c1]
    rivc, rwyc, syc = riv[sl], rwy[sl], sy[sl]

    _, idx = distance_transform_edt(~riv, return_indices=True)
    fill = rwy[idx[0], idx[1]].astype(np.int16)
    med = median_filter(fill, size=3)
    incoh = (riv & (med != rwy))[sl]

    fig, axes = plt.subplots(1, 3, figsize=(21, 8))
    lv = np.where(rivc, rwyc, np.nan)
    im0 = axes[0].imshow(lv, cmap="turbo", interpolation="nearest")
    axes[0].set_title(f"({tx},{tz}) water_y bands "
                      f"[{int(np.nanmin(lv))}..{int(np.nanmax(lv))}]")
    plt.colorbar(im0, ax=axes[0], shrink=0.7)

    dep = np.where(rivc, rwyc - syc, np.nan)
    im1 = axes[1].imshow(dep, cmap="viridis", vmin=0, vmax=6,
                         interpolation="nearest")
    axes[1].set_title("depth (rwy - bed)")
    plt.colorbar(im1, ax=axes[1], shrink=0.7)

    axes[2].imshow(lv, cmap="turbo", interpolation="nearest", alpha=0.55)
    yy, xx = np.where(incoh)
    axes[2].scatter(xx, yy, s=4, c="red")
    axes[2].set_title(f"incoherent cells: {int(incoh.sum())} (red)")
    plt.tight_layout()
    plt.savefig(out, dpi=130)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
