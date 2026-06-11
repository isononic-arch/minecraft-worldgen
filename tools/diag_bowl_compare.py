"""diag_bowl_compare.py — S93b: side-by-side lake-bowl terrace visual.

Renders sy_final as discrete Y-level bands (each integer bed level = one
color) cropped to the lake bbox, for two SURF_DUMP_STEP9_DIR dumps. The
bowl-warp gate: BEFORE shows long straight axis-aligned terrace ledges,
AFTER should show the same terraces arcing/wobbling with the shoreline.

Usage:
  py tools/diag_bowl_compare.py <dirA> <labelA> <dirB> <labelB> <tx> <tz> <out.png>
"""
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def lake_crop(d, tx, tz):
    sy = np.load(f"{d}/sy_final_{tx}_{tz}.npy").astype(np.int32)
    rwy = np.load(f"{d}/rwy_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{d}/rmeta9_{tx}_{tz}.npy")
    lake = (rm == 3) & (rwy > 0)
    if not lake.any():
        lake = (rm > 0) & (rwy > 0)
    rr, cc = np.where(lake)
    r0, r1 = max(rr.min() - 16, 0), min(rr.max() + 16, sy.shape[0])
    c0, c1 = max(cc.min() - 16, 0), min(cc.max() + 16, sy.shape[1])
    return sy[r0:r1, c0:c1], lake[r0:r1, c0:c1], (r0, c0)


def main():
    dA, labA, dB, labB = sys.argv[1:5]
    tx, tz = int(sys.argv[5]), int(sys.argv[6])
    out = sys.argv[7]
    fig, axes = plt.subplots(1, 2, figsize=(16, 8))
    for ax, d, lab in ((axes[0], dA, labA), (axes[1], dB, labB)):
        sy, lake, _ = lake_crop(d, tx, tz)
        show = np.where(lake, sy, np.nan)
        lo = np.nanpercentile(show, 1)
        hi = np.nanpercentile(show, 99)
        im = ax.imshow(show, cmap="turbo", vmin=lo, vmax=hi,
                       interpolation="nearest")
        ax.contour(np.where(lake, sy, sy.max() + 99),
                   levels=np.arange(lo, hi + 1), colors="k",
                   linewidths=0.3, alpha=0.5)
        ax.set_title(f"{lab} — ({tx},{tz}) bed levels (1 band = 1 Y)")
        plt.colorbar(im, ax=ax, shrink=0.7)
    plt.tight_layout()
    plt.savefig(out, dpi=130)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
