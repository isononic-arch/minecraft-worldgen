"""
diag_river_should_connect.py — Show: (a) where river centerlines currently
END, (b) where they SHOULD have ended (nearest visible water cell), and
(c) the extension line that needs to be added to make the river touch
the real lake.

Usage:
    py tools/diag_river_should_connect.py --tile-x 51 --tile-z 53 \
        --out memory/river_should_connect_51_53.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib.pyplot as plt
from scipy.ndimage import label, distance_transform_edt

TILE_SIZE = 512
SEA_LEVEL_RAW = 17050


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, required=True)
    p.add_argument("--tile-z", type=int, required=True)
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--pad", type=int, default=64,
                   help="Halo (in cells) read around tile so off-tile river/lake cells "
                        "are visible during routing")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    masks_dir = Path(args.masks)
    tx, tz = args.tile_x, args.tile_z
    pad = args.pad

    x0, z0 = tx * TILE_SIZE - pad, tz * TILE_SIZE - pad
    w, h = TILE_SIZE + 2 * pad, TILE_SIZE + 2 * pad
    win = Window(x0, z0, w, h)

    def _read(name):
        with rasterio.open(str(masks_dir / f"{name}.tif")) as src:
            return src.read(1, window=win, boundless=True, fill_value=0)

    height = _read("height")
    lake_id = _read("hydro_lake").astype(np.uint16)
    lake_wl_norm = _read("hydro_lake_wl").astype(np.float32)
    centerline = _read("hydro_centerline")  # uint8 — paint widths via diag elsewhere

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
    river = centerline > 0

    # Hillshade base
    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1)

    # Render dry-basin shore in orange (the bad zone), real lake blue, rivers blue
    img = base.copy()
    img[basin & ~underwater] = [0.95, 0.55, 0.20, ]  # orange shore
    img[underwater] = [0.20, 0.50, 0.80]   # blue real lake
    img[river] = [0.10, 0.40, 0.85]        # blue rivers

    # ── Find river endpoints that DON'T touch the real lake ────────────
    # An endpoint = river cell with only 1 river neighbour AND not adjacent
    # to underwater.
    from scipy.ndimage import generic_filter

    # Count river neighbours via 3x3 sum minus self
    river_u8 = river.astype(np.uint8)
    kernel = np.ones((3, 3), dtype=np.uint8)
    from scipy.signal import convolve2d
    nbr_count = convolve2d(river_u8, kernel, mode="same", boundary="fill") - river_u8
    endpoints_all = river & (nbr_count <= 1)

    # An endpoint is "stranded" if it's NOT adjacent to underwater
    # (3x3 dilation of underwater).
    underwater_dil = convolve2d(underwater.astype(np.uint8), kernel,
                                mode="same", boundary="fill") > 0
    stranded = endpoints_all & ~underwater_dil

    # ── For each stranded endpoint, find nearest underwater cell ────────
    # EDT from non-underwater → underwater
    if underwater.any():
        _, idx = distance_transform_edt(~underwater, return_indices=True)
        target_r = idx[0]
        target_c = idx[1]
    else:
        target_r = target_c = None

    # ── Plot ─────────────────────────────────────────────────────────────
    # Crop back to inner tile for display
    crop = slice(pad, pad + TILE_SIZE)
    img_crop = img[crop, crop]

    fig, ax = plt.subplots(1, 1, figsize=(9, 9), facecolor="white")
    ax.imshow(img_crop, interpolation="nearest")

    # Draw extension lines from stranded endpoints to nearest underwater
    n_extensions = 0
    if target_r is not None:
        sr_arr, sc_arr = np.where(stranded)
        for sr, sc in zip(sr_arr, sc_arr):
            tr = int(target_r[sr, sc])
            tc = int(target_c[sr, sc])
            # Coords relative to inner tile
            sr_in = sr - pad
            sc_in = sc - pad
            tr_in = tr - pad
            tc_in = tc - pad
            # Skip if endpoint outside inner tile
            if not (0 <= sr_in < TILE_SIZE and 0 <= sc_in < TILE_SIZE):
                continue
            # Draw red line — clip target to inner tile if needed
            ax.plot([sc_in, tc_in], [sr_in, tr_in],
                    color="red", linewidth=1.5, alpha=0.9)
            ax.scatter([sc_in], [sr_in], color="red", s=18, zorder=10)
            ax.scatter([tc_in], [tr_in], color="lime", s=18, zorder=10)
            n_extensions += 1

    ax.set_title(
        f"Tile ({tx},{tz}) — RIVER → REAL LAKE connectivity\n"
        f"blue = current rivers + real lake | orange = dropped basin shore\n"
        f"red dot = stranded river end | green dot = nearest visible-water cell\n"
        f"red line = extension that would close the gap   ({n_extensions} ends)",
        fontsize=11
    )
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(args.out, dpi=140, bbox_inches="tight")
    print(f"Saved {args.out}", file=sys.stderr)
    print(f"Stranded endpoints: {n_extensions}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
