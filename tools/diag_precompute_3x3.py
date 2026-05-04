"""
diag_precompute_3x3.py — Render a 3x3 precompute view stitched into a single
1536x1536 image: heightmap hillshade + hydro_centerline + hydro_width
footprint + REAL terrain-intersection lakes (height < lake_wl).

Reuses render_precompute_view() from diag_compare_precompute_carve.py.

Usage:
    py tools/diag_precompute_3x3.py --tile-x 51 --tile-z 53 --out memory/precompute_3x3_51_53.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diag_compare_precompute_carve import render_precompute_view

TILE_SIZE = 512


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, required=True,
                   help="Center tile X")
    p.add_argument("--tile-z", type=int, required=True,
                   help="Center tile Z")
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    masks_dir = Path(args.masks)
    cx, cz = args.tile_x, args.tile_z

    print(f"Rendering 3x3 precompute view centered on ({cx}, {cz})...",
          file=sys.stderr)

    # Build 1536x1536 output
    big = np.zeros((TILE_SIZE * 3, TILE_SIZE * 3, 3), dtype=np.uint8)

    for dz in (-1, 0, 1):
        for dx in (-1, 0, 1):
            tx, tz = cx + dx, cz + dz
            print(f"  tile ({tx}, {tz})...", file=sys.stderr)
            tile_rgb = render_precompute_view(masks_dir, tx, tz)
            r0 = (dz + 1) * TILE_SIZE
            c0 = (dx + 1) * TILE_SIZE
            big[r0:r0 + TILE_SIZE, c0:c0 + TILE_SIZE] = tile_rgb

    # Save with tile labels
    fig, ax = plt.subplots(1, 1, figsize=(12, 12), facecolor="white")
    ax.imshow(big, interpolation="nearest")
    ax.set_xticks([])
    ax.set_yticks([])

    # Draw tile-boundary grid
    for i in (TILE_SIZE, 2 * TILE_SIZE):
        ax.axhline(i, color="white", linewidth=0.5, alpha=0.5)
        ax.axvline(i, color="white", linewidth=0.5, alpha=0.5)

    # Tile labels in each cell corner
    for dz in (-1, 0, 1):
        for dx in (-1, 0, 1):
            tx, tz = cx + dx, cz + dz
            r0 = (dz + 1) * TILE_SIZE
            c0 = (dx + 1) * TILE_SIZE
            ax.text(c0 + 8, r0 + 24, f"({tx},{tz})",
                    color="white", fontsize=10,
                    bbox=dict(facecolor="black", alpha=0.6, pad=2,
                              edgecolor="none"))

    ax.set_title(
        f"Precompute view 3x3 — centered on ({cx},{cz}) — "
        f"hillshade + centerlines + width footprint + terrain-intersection lakes",
        fontsize=11
    )
    fig.tight_layout()
    fig.savefig(args.out, dpi=140, bbox_inches="tight")
    print(f"Saved {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
