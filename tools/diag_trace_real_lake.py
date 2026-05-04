"""
diag_trace_real_lake.py — Show step-by-step how to derive the real
in-game lake from the precompute basin: trace the height==lake_wl
contour, fill the inside, done.

Usage:
    py tools/diag_trace_real_lake.py --tile-x 51 --tile-z 53 \
        --out memory/trace_real_lake_51_53.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib.pyplot as plt

TILE_SIZE = 512
SEA_LEVEL_RAW = 17050


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, required=True)
    p.add_argument("--tile-z", type=int, required=True)
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    masks_dir = Path(args.masks)
    tx, tz = args.tile_x, args.tile_z
    x0, z0 = tx * TILE_SIZE, tz * TILE_SIZE
    win = Window(x0, z0, TILE_SIZE, TILE_SIZE)

    def _read(name):
        with rasterio.open(str(masks_dir / f"{name}.tif")) as src:
            return src.read(1, window=win)

    height = _read("height")
    lake_id = _read("hydro_lake").astype(np.uint16)
    lake_wl_norm = _read("hydro_lake_wl").astype(np.float32)

    gaea_in = np.array([0, SEA_LEVEL_RAW, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height_blocks = np.interp(
        height.ravel(), gaea_in, mc_y_out
    ).reshape(height.shape).astype(np.float32)
    lake_wl_mc = np.interp(
        (lake_wl_norm * 65535.0).ravel(), gaea_in, mc_y_out
    ).reshape(lake_wl_norm.shape).astype(np.float32)

    basin = (lake_id > 0) & (height > SEA_LEVEL_RAW)
    underwater = basin & (height_blocks < lake_wl_mc)

    # Hillshade base
    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1)

    fig, axes = plt.subplots(1, 4, figsize=(20, 5.2), facecolor="white")

    # Panel 1: precompute basin
    img1 = base.copy()
    img1[basin] = [0.95, 0.55, 0.20]
    axes[0].imshow(img1, interpolation="nearest")
    axes[0].set_title("1) Precompute basin\n(hydro_lake.tif > 0)\n8x8 NEAREST staircase, oversized",
                      fontsize=10)

    # Panel 2: same + terrain contour at water level (the "trace line")
    img2 = base.copy()
    img2[basin] = [0.95, 0.55, 0.20]
    axes[1].imshow(img2, interpolation="nearest")
    # Contour at height == wl_mc — the natural shoreline
    if basin.any() and underwater.any():
        wl_value = float(np.median(lake_wl_mc[basin]))
        axes[1].contour(height_blocks, levels=[wl_value],
                        colors=["#1064c8"], linewidths=2.0)
    axes[1].set_title(f"2) Trace the height={wl_value:.1f} contour\n"
                      f"(natural shoreline of the lake)",
                      fontsize=10)

    # Panel 3: fill inside the contour AND inside basin
    img3 = base.copy()
    img3[basin & ~underwater] = [0.95, 0.55, 0.20]  # dry shore = orange
    img3[underwater]          = [0.20, 0.50, 0.80]  # blue real lake
    axes[2].imshow(img3, interpolation="nearest")
    axes[2].set_title("3) Fill inside (basin & terrain<wl)\nblue = real in-game lake\norange = dropped",
                      fontsize=10)

    # Panel 4: just the real lake on hillshade
    img4 = base.copy()
    img4[underwater] = [0.20, 0.50, 0.80]
    axes[3].imshow(img4, interpolation="nearest")
    axes[3].set_title("4) Result: real lake mask\nuse THIS everywhere downstream",
                      fontsize=10)

    for ax in axes:
        ax.set_xticks([]); ax.set_yticks([])

    fig.suptitle(
        f"Tracing the precompute basin → real in-game lake — "
        f"tile ({tx},{tz})",
        fontsize=12
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(args.out, dpi=140, bbox_inches="tight")
    print(f"Saved {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
